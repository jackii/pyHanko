"""
.. versionchanged: 0.3.0
    Added support for PDF 2.0 encryption standards and crypt filters.

Utilities for PDF encryption. This module covers all methods outlined in the
standard:

* Legacy RC4-based encryption (based on PyPDF2 code).
* AES-128 encryption with legacy key derivation (partly based on PyPDF2 code).
* PDF 2.0 AES-256 encryption.
* Public key encryption backed by any of the above.

Following the language in the standard, encryption operations are backed by
subclasses of the :class:`SecurityHandler` class, which provides a more or less
generic API.

.. danger::
    The members of this module are all considered internal API, and are
    therefore subject to change without notice.

.. danger::
    One should also be aware that the legacy encryption scheme implemented
    here is (very) weak, and we only support it for compatibility reasons.
    Under no circumstances should it still be used to encrypt new files.


About crypt filters
-------------------

Crypt filters are objects that handle encryption and decryption of streams and
strings, either for all of them, or for a specific subset (e.g. streams
representing embedded files). In the context of the PDF standard, crypt filters
are a notion that only makes sense for security handlers of version 4 and up.
In pyHanko, however, *all* encryption and decryption operations pass through
crypt filters, and the serialisation/deserialisation logic in
:class:`SecurityHandler` and its subclasses transparently deals with staying
backwards compatible with earlier revisions.

Internally, pyHanko loosely distinguishes between implicit and explicit
uses of crypt filters:

* Explicit crypt filters are used by directly referring to them from the
  ``/Filter`` entry of a stream dictionary. These are invoked in the usual
  stream decoding process.
* Implicit crypt filters are set by the ``/StmF`` and ``/StrF`` entries
  in the security handler's crypt filter configuration, and are invoked by the
  object reading/writing procedures as necessary. These filters are invisble
  to the stream encoding/decoding process: the
  :attr:`~.generic.StreamObject.encoded_data` attribute of
  an "implicitly encrypted" stream will therefore contain decrypted data ready
  to be decoded in the usual way.

As long as you don't require access to encoded object data and/or raw encrypted
object data, this distiction should be irrelevant to you as an API user.
"""

import logging
import abc
import struct
import secrets
import enum
from dataclasses import dataclass
from hashlib import md5, sha256, sha384, sha512, sha1
from typing import Dict, Type, Optional, Tuple, Union, List, Set

from asn1crypto import x509, cms
from asn1crypto.algos import EncryptionAlgorithmId
from asn1crypto.keys import PublicKeyAlgorithm, PrivateKeyInfo
from oscrypto import symmetric, asymmetric, keys as oskeys

from . import generic, misc

__all__ = [
    'SecurityHandler', 'StandardSecurityHandler', 'PubKeySecurityHandler',
    'SecurityHandlerVersion', 'StandardSecuritySettingsRevision',
    'PubKeyAdbeSubFilter', 'CryptFilterConfiguration', 'CryptFilter',
    'StandardCryptFilter', 'PubKeyCryptFilter', 'IdentityCryptFilter',
    'RC4CryptFilterMixin', 'AESCryptFilterMixin', 'StandardAESCryptFilter',
    'StandardRC4CryptFilter', 'PubKeyAESCryptFilter', 'PubKeyRC4CryptFilter',
    'EnvelopeKeyDecrypter', 'SimpleEnvelopeKeyDecrypter',
    'STD_CF', 'DEFAULT_CRYPT_FILTER', 'IDENTITY',
    'legacy_derive_object_key'
]

logger = logging.getLogger(__name__)

# ref: pdf1.8 spec section 3.5.2 algorithm 3.2
_encryption_padding = (
    b'\x28\xbf\x4e\x5e\x4e\x75\x8a\x41\x64\x00\x4e\x56'
    b'\xff\xfa\x01\x08\x2e\x2e\x00\xb6\xd0\x68\x3e\x80\x2f\x0c'
    b'\xa9\xfe\x64\x53\x69\x7a'
)


# Implementation of algorithm 3.2 of the PDF standard security handler,
# section 3.5.2 of the PDF 1.6 reference.
def _derive_legacy_file_key(password, rev, keylen, owner_entry, p_entry,
                            id1_entry, metadata_encrypt=True):
    # 1. Pad or truncate the password string to exactly 32 bytes.  If the
    # password string is more than 32 bytes long, use only its first 32 bytes;
    # if it is less than 32 bytes long, pad it by appending the required number
    # of additional bytes from the beginning of the padding string
    # (_encryption_padding).
    password = (password + _encryption_padding)[:32]
    # 2. Initialize the MD5 hash function and pass the result of step 1 as
    # input to this function.
    m = md5(password)
    # 3. Pass the value of the encryption dictionary's /O entry to the MD5 hash
    # function.
    m.update(owner_entry)
    # 4. Treat the value of the /P entry as an unsigned 4-byte integer and pass
    # these bytes to the MD5 hash function, low-order byte first.
    p_entry = struct.pack('<i', p_entry)
    m.update(p_entry)
    # 5. Pass the first element of the file's file identifier array to the MD5
    # hash function.
    m.update(id1_entry)
    # 6. (Revision 4 or greater) If document metadata is not being encrypted,
    # pass 4 bytes with the value 0xFFFFFFFF to the MD5 hash function.
    if rev >= 4 and not metadata_encrypt:
        m.update(b"\xff\xff\xff\xff")
    # 7. Finish the hash.
    md5_hash = m.digest()
    # 8. (Revision 3 or greater) Do the following 50 times: Take the output
    # from the previous MD5 hash and pass the first n bytes of the output as
    # input into a new MD5 hash, where n is the number of bytes of the
    # encryption key as defined by the value of the encryption dictionary's
    # /Length entry.
    if rev >= 3:
        for i in range(50):
            md5_hash = md5(md5_hash[:keylen]).digest()
    # 9. Set the encryption key to the first n bytes of the output from the
    # final MD5 hash, where n is always 5 for revision 2 but, for revision 3 or
    # greater, depends on the value of the encryption dictionary's /Length
    # entry.
    return md5_hash[:keylen]


@dataclass
class _R6KeyEntry:
    hash_value: bytes
    validation_salt: bytes
    key_salt: bytes

    @classmethod
    def from_bytes(cls, entry: bytes) -> '_R6KeyEntry':
        assert len(entry) == 48
        return _R6KeyEntry(entry[:32], entry[32:40], entry[40:48])


def _legacy_normalise_pw(password: Union[str, bytes]) -> bytes:
    if isinstance(password, str):
        return generic.encode_pdfdocencoding(password[:32])
    else:
        return password[:32]


def _r6_normalise_pw(password: Union[str, bytes]) -> bytes:
    if isinstance(password, str):
        from ._saslprep import saslprep
        password = saslprep(password).encode('utf-8')
    return password[:127]


def _r6_password_authenticate(pw_bytes: bytes, entry: _R6KeyEntry,
                              u_entry: Optional[bytes] = None):
    purported_hash = _r6_hash_algo(pw_bytes, entry.validation_salt, u_entry)
    return purported_hash == entry.hash_value


def _r6_derive_file_key(pw_bytes: bytes, entry: _R6KeyEntry, e_entry: bytes,
                        u_entry: Optional[bytes] = None):
    interm_key = _r6_hash_algo(pw_bytes, entry.key_salt, u_entry)
    assert len(e_entry) == 32
    return symmetric.aes_cbc_no_padding_decrypt(
        key=interm_key, data=e_entry, iv=bytes(16)
    )


_EXPECTED_PERMS_8 = {
    0x54: True,  # 'T'
    0x46: False  # 'F'
}


def _bytes_mod_3(input_bytes: bytes):
    # 256 is 1 mod 3, so we can just sum 'em
    return sum(b % 3 for b in input_bytes) % 3


# Algorithm 2.B in ISO 32000-2 § 7.6.4.3.4
def _r6_hash_algo(pw_bytes: bytes, current_salt: bytes,
                  u_entry: Optional[bytes] = None) -> bytes:
    initial_hash = sha256(pw_bytes)
    assert len(current_salt) == 8
    initial_hash.update(current_salt)
    if u_entry:
        assert len(u_entry) == 48
        initial_hash.update(u_entry)
    k = initial_hash.digest()
    hashes = (sha256, sha384, sha512)
    round_no = last_byte_val = 0
    while round_no < 64 or last_byte_val > round_no - 32:
        k1 = (pw_bytes + k + (u_entry or b'')) * 64
        e = symmetric.aes_cbc_no_padding_encrypt(
            key=k[:16], data=k1, iv=k[16:32]
        )[1]
        # compute the first 16 bytes of e, interpreted as an unsigned integer
        # mod 3
        next_hash = hashes[_bytes_mod_3(e[:16])]
        k = next_hash(e).digest()
        last_byte_val = e[len(e) - 1]
        round_no += 1
    return k[:32]


# Implementation of algorithm 3.3 of the PDF standard security handler,
# section 3.5.2 of the PDF 1.6 reference.
def _compute_o_value_legacy(owner_pwd, user_pwd, rev, keylen):
    # steps 1 - 4
    key = _compute_o_value_legacy_prep(owner_pwd, rev, keylen)
    # 5. Pad or truncate the user password string as described in step 1 of
    # algorithm 3.2.
    user_pwd = (user_pwd + _encryption_padding)[:32]
    # 6. Encrypt the result of step 5, using an RC4 encryption function with
    # the encryption key obtained in step 4.
    val = symmetric.rc4_encrypt(key, user_pwd)
    # 7. (Revision 3 or greater) Do the following 19 times: Take the output
    # from the previous invocation of the RC4 function and pass it as input to
    # a new invocation of the function; use an encryption key generated by
    # taking each byte of the encryption key obtained in step 4 and performing
    # an XOR operation between that byte and the single-byte value of the
    # iteration counter (from 1 to 19).
    if rev >= 3:
        for i in range(1, 20):
            new_key = bytes(b ^ i for b in key)
            val = symmetric.rc4_encrypt(new_key, val)
    # 8. Store the output from the final invocation of the RC4 as the value of
    # the /O entry in the encryption dictionary.
    return val


# Steps 1-4 of algorithm 3.3
def _compute_o_value_legacy_prep(password, rev, keylen):
    # 1. Pad or truncate the owner password string as described in step 1 of
    # algorithm 3.2.  If there is no owner password, use the user password
    # instead.
    password = (password + _encryption_padding)[:32]
    # 2. Initialize the MD5 hash function and pass the result of step 1 as
    # input to this function.
    m = md5(password)
    # 3. (Revision 3 or greater) Do the following 50 times: Take the output
    # from the previous MD5 hash and pass it as input into a new MD5 hash.
    md5_hash = m.digest()
    if rev >= 3:
        for i in range(50):
            md5_hash = md5(md5_hash).digest()
    # 4. Create an RC4 encryption key using the first n bytes of the output
    # from the final MD5 hash, where n is always 5 for revision 2 but, for
    # revision 3 or greater, depends on the value of the encryption
    # dictionary's /Length entry.
    key = md5_hash[:keylen]
    return key


# Implementation of algorithm 3.4 of the PDF standard security handler,
# section 3.5.2 of the PDF 1.6 reference.
def _compute_u_value_r2(password, owner_entry, p_entry, id1_entry):
    # 1. Create an encryption key based on the user password string, as
    # described in algorithm 3.2.
    key = _derive_legacy_file_key(password, 2, 5, owner_entry, p_entry,
                                  id1_entry)
    # 2. Encrypt the 32-byte padding string shown in step 1 of algorithm 3.2,
    # using an RC4 encryption function with the encryption key from the
    # preceding step.
    u = symmetric.rc4_encrypt(key, _encryption_padding)
    # 3. Store the result of step 2 as the value of the /U entry in the
    # encryption dictionary.
    return u, key


# Implementation of algorithm 3.4 of the PDF standard security handler,
# section 3.5.2 of the PDF 1.6 reference.
def _compute_u_value_r34(password, rev, keylen, owner_entry, p_entry,
                         id1_entry):
    # 1. Create an encryption key based on the user password string, as
    # described in Algorithm 3.2.
    key = _derive_legacy_file_key(password, rev, keylen, owner_entry, p_entry,
                                  id1_entry)
    # 2. Initialize the MD5 hash function and pass the 32-byte padding string
    # shown in step 1 of Algorithm 3.2 as input to this function.
    m = md5()
    m.update(_encryption_padding)
    # 3. Pass the first element of the file's file identifier array (the value
    # of the ID entry in the document's trailer dictionary; see Table 3.13 on
    # page 73) to the hash function and finish the hash.  (See implementation
    # note 25 in Appendix H.)
    m.update(id1_entry)
    md5_hash = m.digest()
    # 4. Encrypt the 16-byte result of the hash, using an RC4 encryption
    # function with the encryption key from step 1.
    val = symmetric.rc4_encrypt(key, md5_hash)
    # 5. Do the following 19 times: Take the output from the previous
    # invocation of the RC4 function and pass it as input to a new invocation
    # of the function; use an encryption key generated by taking each byte of
    # the original encryption key (obtained in step 2) and performing an XOR
    # operation between that byte and the single-byte value of the iteration
    # counter (from 1 to 19).
    for i in range(1, 20):
        new_key = bytes(b ^ i for b in key)
        val = symmetric.rc4_encrypt(new_key, val)
    # 6. Append 16 bytes of arbitrary padding to the output from the final
    # invocation of the RC4 function and store the 32-byte result as the value
    # of the U entry in the encryption dictionary.
    # (implementer note: I don't know what "arbitrary padding" is supposed to
    # mean, so I have used null bytes.  This seems to match a few other
    # people's implementations)
    return val + (b'\x00' * 16), key


def legacy_derive_object_key(shared_key: bytes, idnum: int, generation: int,
                             use_aes=False) -> bytes:
    """
    Function that does the key derivation for PDF's legacy security handlers.

    :param shared_key:
        Global file encryption key.
    :param idnum:
        ID of the object being written.
    :param generation:
        Generation number of the object being written.
    :param use_aes:
        Boolean indicating whether the security handler uses RC4 or AES(-128).
    :return:
    """
    pack1 = struct.pack("<i", idnum)[:3]
    pack2 = struct.pack("<i", generation)[:2]
    key = shared_key + pack1 + pack2
    assert len(key) == (len(shared_key) + 5)
    if use_aes:
        key += b'sAlT'
    md5_hash = md5(key).digest()
    return md5_hash[:min(16, len(shared_key) + 5)]


class AuthResult(misc.OrderedEnum):
    """
    Describes the result of an authentication
    """
    FAILED = 0
    USER = 1
    OWNER = 2


@enum.unique
class SecurityHandlerVersion(misc.OrderedEnum):
    """
    Indicates the security handler's version.

    The enum constants are named more or less in accordance with the
    cryptographic algorithms they permit.
    """
    RC4_40 = 1
    RC4_LONGER_KEYS = 2
    RC4_OR_AES128 = 4
    AES256 = 5

    OTHER = None
    """
    Placeholder value for custom security handlers.
    """


class SecurityHandler:
    """
    Generic PDF security handler interface.

    This class contains relatively little actual functionality, except for
    some common initialisation logic and bookkeeping machinery to register
    security handler implementations.

    :param version:
        Indicates the version of the security handler to use, as described
        in the specification. See :class:`.SecurityHandlerVersion`.
    :param legacy_keylen:
        Key length in bytes (only relevant for legacy encryption handlers).
    :param crypt_filter_config:
        The crypt filter configuration for the security handler, in the
        form of a :class:`.CryptFilterConfiguration` object.

        .. note::
            PyHanko implements legacy security handlers (which, according to
            the standard, aren't crypt filter-aware) using crypt filters
            as well, even though they aren't serialised to the output file.
    :param encrypt_metadata:
        Flag indicating whether document (XMP) metadata is to be encrypted.

        .. warning::
            Currently, PyHanko does not manage metadata streams, so until
            that changes, it is the responsibility of the API user to mark
            metadata streams using the `/Identity` crypt filter as required.

            Nonetheless, the value of this flag is required in key derivation
            computations, so the security handler needs to know about it.
    """

    __registered_subclasses: Dict[str, Type['SecurityHandler']] = dict()

    def __init__(self, version: SecurityHandlerVersion, legacy_keylen,
                 crypt_filter_config: 'CryptFilterConfiguration',
                 encrypt_metadata=True):
        self.version = version
        if version == SecurityHandlerVersion.RC4_40:
            legacy_keylen = 5
        elif not (5 <= legacy_keylen <= 16) \
                and version <= SecurityHandlerVersion.RC4_OR_AES128:
            raise misc.PdfError("Key length must be between 5 and 16")
        elif version == SecurityHandlerVersion.AES256:
            legacy_keylen = 32

        if crypt_filter_config is None:
            raise misc.PdfError("No crypt filter configuration")
        crypt_filter_config.set_security_handler(self)

        self.keylen = legacy_keylen
        self.crypt_filter_config = crypt_filter_config
        self.encrypt_metadata = encrypt_metadata

    @staticmethod
    def register(cls: Type['SecurityHandler']):
        """
        Register a security handler class.
        Intended to be used as a decorator on subclasses.

        See :meth:`build` for further information.

        :param cls:
            A subclass of :class:`.SecurityHandler`.
        """
        # don't put this in __init_subclass__, so that people can inherit from
        # security handlers if they want
        SecurityHandler.__registered_subclasses[cls.get_name()] = cls
        return cls

    @staticmethod
    def build(encrypt_dict: generic.DictionaryObject) -> 'SecurityHandler':
        """
        Instantiate an appropriate :class:`.SecurityHandler` from a PDF
        document's encryption dictionary.

        PyHanko will search the registry for a security handler with
        a name matching the ``/Filter`` entry. Failing that, a security
        handler implementing the protocol designated by the
        ``/SubFilter`` entry (see :meth:`support_generic_subfilters`) will be
        chosen.

        Once an appropriate :class:`.SecurityHandler` subclass has been
        selected, pyHanko will invoke the subclass's
        :meth:`instantiate_from_pdf_object` method with the original encryption
        dictionary as its argument.

        :param encrypt_dict:
            A PDF encryption dictionary.
        :return:
        """
        handler_name = encrypt_dict.get('/Filter', '/Standard')
        try:
            cls = SecurityHandler.__registered_subclasses[handler_name]
        except KeyError:
            # no handler with that exact name, but if the encryption dictionary
            # specifies a generic /SubFilter, we can still try to look for an
            # alternative.
            try:
                subfilter = encrypt_dict['/SubFilter']
            except KeyError:
                raise misc.PdfReadError(
                    f"There is no security handler named {handler_name}, "
                    f"and the encryption dictionary does not contain a generic "
                    f"/SubFilter entry."
                )
            try:
                cls = next(
                    h for h in SecurityHandler.__registered_subclasses.values()
                    if subfilter in h.support_generic_subfilters()
                )
            except StopIteration:
                raise misc.PdfReadError(
                    f"There is no security handler named {handler_name}, and "
                    f"none of the available handlers support the declared "
                    f"/SubFilter {subfilter}."
                )

        return cls.instantiate_from_pdf_object(encrypt_dict)

    @classmethod
    def get_name(cls) -> str:
        """
        Retrieves the name of this security handler.

        :return:
            The name of this security handler.
        """
        raise NotImplementedError

    @classmethod
    def support_generic_subfilters(cls) -> Set[str]:
        """
        Indicates the generic ``/SubFilter`` values that this security handler
        supports.

        :return:
            A set of generic protocols (indicated in the ``/SubFilter`` entry
            of an encryption dictionary) that this :class:`.SecurityHandler`
            class implements. Defaults to the empty set.
        """
        return set()

    @classmethod
    def instantiate_from_pdf_object(cls,
                                    encrypt_dict: generic.DictionaryObject):
        """
        Instantiate an object of this class using a PDF encryption dictionary
        as input.

        :param encrypt_dict:
            A PDF encryption dictionary.
        :return:
        """
        raise NotImplementedError

    def as_pdf_object(self) -> generic.DictionaryObject:
        """
        Serialise this security handler to a PDF encryption dictionary.

        :return:
            A PDF encryption dictionary.
        """
        raise NotImplementedError

    def authenticate(self, credential, id1=None) -> AuthResult:
        """
        Authenticate a credential holder with this security handler.

        :param credential:
            A credential.
            The type of the credential is left up to the subclasses.
        :param id1:
            The first part of the document ID of the document being accessed.
        :return:
            A :class:`AuthResult` object indicating the level of access
            obtained.
        """
        raise NotImplementedError

    def get_string_filter(self) -> 'CryptFilter':
        """
        :return:
            The crypt filter responsible for decrypting strings
            for this security handler.
        """
        return self.crypt_filter_config.get_for_string()

    def get_stream_filter(self, name=None) -> 'CryptFilter':
        """
        :param name:
            Optionally specify a crypt filter by name.
        :return:
            The default crypt filter responsible for decrypting streams
            for this security handler, or the crypt filter named ``name``,
            if not ``None``.
        """
        if name is None:
            return self.crypt_filter_config.get_for_stream()
        return self.crypt_filter_config[name]


@enum.unique
class StandardSecuritySettingsRevision(misc.OrderedEnum):
    """Indicate the standard security handler revision to emulate."""

    RC4_BASIC = 2
    RC4_EXTENDED = 3
    RC4_OR_AES128 = 4
    AES256 = 6


ALL_PERMS = -4
"""
Dummy value that translates to "everything is allowed" in an
encrypted PDF document.
"""


class CryptFilter:
    """
    Generic abstract crypt filter class.

    The superclass only handles the binding with the security handler, and
    offers some default implementations for serialisation routines that may
    be overridden in subclasses.

    There is generally no requirement for crypt filters to be compatible with
    *any* security handler (the leaf classes in this module aren't), but
    the API supports mixin usage so code can be shared.
    """

    _handler: 'SecurityHandler' = None
    _shared_key: Optional[bytes] = None

    def _set_security_handler(self, handler):
        """
        Set the security handler to which this crypt filter is tied.

        Called by pyHanko during initialisation.
        """
        self._handler = handler
        self._shared_key = None

    @property
    def _auth_failed(self) -> bool:
        """
        Indicate whether authentication previously failed for this crypt filter.

        Note that re-authenticating is not forbidden, this function mostly
        exists to make error reporting easier.

        Crypt filters are allowed to manage their own authentication, but may
        defer to the security handler as well.
        """
        raise NotImplementedError

    @property
    def method(self) -> generic.NameObject:
        """
        :return:
            The method name (``/CFM`` entry) associated with this crypt filter.
        """
        raise NotImplementedError

    @property
    def keylen(self) -> int:
        """
        :return:
            The keylength (in bytes) of the key associated with this crypt
            filter.
        """
        raise NotImplementedError

    def encrypt(self, key, plaintext: bytes, params=None) -> bytes:
        """
        Encrypt plaintext with the specified key.

        :param key:
            The current local key, which may or may not be equal to this
            crypt filter's global key.
        :param plaintext:
            Plaintext to encrypt.
        :param params:
            Optional parameters private to the crypt filter,
            specified as a PDF dictionary. These can only be used for
            explicit crypt filters; the parameters are then sourced from
            the corresponding entry in ``/DecodeParms``.
        :return:
            The resulting ciphertext.
        """
        raise NotImplementedError

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Decrypt ciphertext with the specified key.

        :param key:
            The current local key, which may or may not be equal to this
            crypt filter's global key.
        :param ciphertext:
            Ciphertext to decrypt.
        :param params:
            Optional parameters private to the crypt filter,
            specified as a PDF dictionary. These can only be used for
            explicit crypt filters; the parameters are then sourced from
            the corresponding entry in ``/DecodeParms``.
        :return:
            The resulting plaintext.
        """
        raise NotImplementedError

    def as_pdf_object(self) -> generic.DictionaryObject:
        """
        Serialise this crypt filter to a PDF crypt filter dictionary.

        .. note::
            Implementations are encouraged to use a cooperative inheritance
            model, where subclasses first call ``super().as_pdf_object()``
            and add the keys they need before returning the result.

            This makes it easy to write crypt filter mixins that can provide
            functionality to multiple handlers.

        :return:
            A PDF crypt filter dictionary.
        """
        result = generic.DictionaryObject({
            # TODO handle /AuthEvent properly
            generic.NameObject('/AuthEvent'): generic.NameObject('/DocOpen'),
            generic.NameObject('/CFM'): self.method
        })
        return result

    def derive_shared_encryption_key(self) -> bytes:
        """
        Compute the (global) file encryption key for this crypt filter.

        :return:
            The key, as a :class:`bytes` object.
        :raise misc.PdfError:
            Raised if the data needed to derive the key is not present (e.g.
            because the caller hasn't authenticated yet).
        """
        raise NotImplementedError

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Derive the encryption key for a specific object, based on the shared
        file encryption key.

        :param idnum:
            ID of the object being encrypted.
        :param generation:
            Generation number of the object being encrypted.
        :return:
            The local key to use for this object.
        """
        raise NotImplementedError

    @property
    def shared_key(self) -> bytes:
        """
        Return the shared file encryption key for this crypt filter, or
        attempt to compute it using :meth:`derive_shared_encryption_key`
        if not available.
        """
        key = self._shared_key
        if key is None:
            if self._auth_failed:
                raise misc.PdfReadError("Authentication failed")
            key = self._shared_key = self.derive_shared_encryption_key()
        return key


class StandardCryptFilter(CryptFilter, abc.ABC):
    _handler: 'StandardSecurityHandler' = None

    @property
    def _auth_failed(self):
        if isinstance(self._handler, StandardSecurityHandler):
            return self._handler._auth_failed
        raise NotImplementedError

    def _set_security_handler(self, handler):
        if not isinstance(handler, StandardSecurityHandler):
            raise TypeError  # pragma: nocover
        super()._set_security_handler(handler)
        self._shared_key = None

    def derive_shared_encryption_key(self) -> bytes:
        return self._handler.get_file_encryption_key()

    def as_pdf_object(self):
        result = super().as_pdf_object()
        # Specifying the length in bytes is wrong per the 2017 spec,
        # but the 2020 revision mandates doing it this way
        result['/Length'] = generic.NumberObject(self.keylen)
        return result


class PubKeyCryptFilter(CryptFilter, abc.ABC):
    _handler: 'PubKeySecurityHandler' = None

    def __init__(self, *, recipients=None, acts_as_default=False,
                 encrypt_metadata=True, **kwargs):
        self.recipients = recipients
        self.acts_as_default = acts_as_default
        self.encrypt_metadata = encrypt_metadata
        self._pubkey_auth_failed = False
        self._shared_key = self._recp_key_seed = None
        super().__init__(**kwargs)

    @property
    def _auth_failed(self) -> bool:
        return self._pubkey_auth_failed

    def _set_security_handler(self, handler):
        if not isinstance(handler, PubKeySecurityHandler):
            raise TypeError  # pragma: nocover
        super()._set_security_handler(handler)
        self._shared_key = self._recp_key_seed = None

    def add_recipients(self, certs: List[x509.Certificate]):
        # this always adds one full CMS object to the Recipients array

        if not self.acts_as_default and self.recipients:
            raise misc.PdfError(
                "A non-default crypt filter cannot have multiple sets of "
                "recipients."
            )
        if self.recipients is None:
            # assume that this is a freshly created pubkey crypt filter,
            # so set up the shared seed
            self._recp_key_seed = secrets.token_bytes(20)
            self.recipients = []

        if self._shared_key is not None or self._recp_key_seed is None:
            raise misc.PdfError(
                "Adding recipients after deriving the shared key or "
                "before authenticating is not possible."
            )
        # TODO allow user to specify perms
        perms = ALL_PERMS
        new_cms = construct_recipient_cms(
            certs, self._recp_key_seed, perms,
            include_permissions=self.acts_as_default
        )
        self.recipients.append(new_cms)

    def authenticate(self, credential) -> AuthResult:
        for recp in self.recipients:
            seed = read_seed_from_recipient_cms(recp, credential)
            if seed is not None:
                self._recp_key_seed = seed
                return AuthResult.USER
        return AuthResult.FAILED

    def derive_shared_encryption_key(self) -> bytes:
        if self._recp_key_seed is None:
            raise misc.PdfError("No seed available; authenticate first.")
        if self._handler.version == SecurityHandlerVersion.AES256:
            md = sha256()
        else:
            md = sha1()
        md.update(self._recp_key_seed)
        for recp in self.recipients:
            md.update(recp.dump())
        if not self.encrypt_metadata:
            md.update(b'\xff\xff\xff\xff')
        return md.digest()[:self.keylen]

    def as_pdf_object(self):
        result = super().as_pdf_object()
        result['/Length'] = generic.NumberObject(self.keylen * 8)
        recipients = generic.ArrayObject(
            generic.ByteStringObject(recp.dump())
            for recp in self.recipients
        )
        if self.acts_as_default:
            result['/Recipients'] = recipients
        else:
            # non-default crypt filters can only have one recipient object
            result['/Recipients'] = recipients[0]
        result['/EncryptMetadata'] \
            = generic.BooleanObject(self.encrypt_metadata)
        return result


class IdentityCryptFilter(CryptFilter, metaclass=misc.Singleton):
    """
    Class implementing the trivial crypt filter.

    This is a singleton class, so all its instances are identical.
    Additionally, some of the :class:`.CryptFilter` API is nonfunctional.
    In particular, :meth:`as_pdf_object` always raises an error, since the
    ``/Identity`` filter cannot be serialised.
    """

    method = generic.NameObject('/None')
    keylen = 0
    _auth_failed = False

    def derive_shared_encryption_key(self) -> bytes:
        """Always returns an empty byte string."""
        return b''  # pragma: nocover

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Always returns an empty byte string.

        :param idnum:
            Ignored.
        :param generation:
            Ignored.
        :return:
        """
        return b''

    def _set_security_handler(self, handler):
        """
        No-op.

        :param handler:
            Ignored.
        :return:
        """
        return

    def as_pdf_object(self):
        """
        Not implemented for this crypt filter.

        :raise misc.PdfError:
            Always.
        """
        raise misc.PdfError("Identity filter cannot be serialised")

    def encrypt(self, key, plaintext: bytes, params=None) -> bytes:
        """
        Identity function.

        :param key:
            Ignored.
        :param plaintext:
            Returned as-is.
        :param params:
            Ignored.
        :return:
            The original plaintext.
        """
        return plaintext

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Identity function.

        :param key:
            Ignored.
        :param ciphertext:
            Returned as-is.
        :param params:
            Ignored.
        :return:
            The original ciphertext.
        """
        return ciphertext


class RC4CryptFilterMixin(CryptFilter, abc.ABC):
    """
    Mixin for RC4-based crypt filters.

    :param keylen:
        Key length, in bytes. Defaults to 5.
    """

    method = generic.NameObject('/V2')
    keylen = None

    def __init__(self, *, keylen=5, **kwargs):
        self.keylen = keylen
        super().__init__(**kwargs)

    def encrypt(self, key, plaintext: bytes, params=None) -> bytes:
        """
        Encrypt data using RC4.

        :param key:
            Local encryption key.
        :param plaintext:
            Plaintext to encrypt.
        :param params:
            Ignored.
        :return:
            Ciphertext.
        """
        return symmetric.rc4_encrypt(key, plaintext)

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Decrypt data using RC4.

        :param key:
            Local encryption key.
        :param ciphertext:
            Ciphertext to decrypt.
        :param params:
            Ignored.
        :return:
            Plaintext.
        """
        return symmetric.rc4_encrypt(key, ciphertext)

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Derive the local key for the given object ID and generation number,
        by calling :func:`.legacy_derive_object_key`.

        :param idnum:
            ID of the object being encrypted.
        :param generation:
            Generation number of the object being encrypted.
        :return:
            The local key.
        """
        return legacy_derive_object_key(self.shared_key, idnum, generation)


class AESCryptFilterMixin(CryptFilter, abc.ABC):
    """Mixin for AES-based crypt filters."""
    keylen = None
    method = None

    def __init__(self, *, keylen, **kwargs):
        if keylen not in (16, 32):
            raise NotImplementedError("Only AES-128 and AES-256 are supported")
        self.keylen = keylen
        self.method = (
            generic.NameObject('/AESV2') if keylen == 16 else
            generic.NameObject('/AESV3')
        )
        super().__init__(**kwargs)

    def encrypt(self, key, plaintext: bytes, params=None):
        """
        Encrypt data using AES in CBC mode, with PKCS#7 padding.

        :param key:
            The key to use.
        :param plaintext:
            The plaintext to be encrypted.
        :param params:
            Ignored.
        :return:
            The resulting ciphertext, prepended with a 16-byte initialisation
            vector.
        """
        iv, ciphertext = symmetric.aes_cbc_pkcs7_encrypt(
            key, plaintext, secrets.token_bytes(16)
        )
        return iv + ciphertext

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Decrypt data using AES in CBC mode, with PKCS#7 padding.

        :param key:
            The key to use.
        :param ciphertext:
            The ciphertext to be decrypted, prepended with a 16-byte
            initialisation vector.
        :param params:
            Ignored.
        :return:
            The resulting plaintext.
        """
        iv, data = ciphertext[:16], ciphertext[16:]
        return symmetric.aes_cbc_pkcs7_decrypt(key, data, iv)

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Derive the local key for the given object ID and generation number.

        If the associated handler is of version
        :attr:`.SecurityHandlerVersion.AES256` or greater, this method
        simply returns the global key as-is.
        If not, the computation is carried out by
        :func:`.legacy_derive_object_key`.

        :param idnum:
            ID of the object being encrypted.
        :param generation:
            Generation number of the object being encrypted.
        :return:
            The local key.
        """
        if self._handler.version >= SecurityHandlerVersion.AES256:
            return self.shared_key
        else:
            return legacy_derive_object_key(
                self.shared_key, idnum, generation, use_aes=True
            )


class StandardAESCryptFilter(StandardCryptFilter, AESCryptFilterMixin):
    """
    AES crypt filter for the standard security handler.
    """
    pass


class PubKeyAESCryptFilter(PubKeyCryptFilter, AESCryptFilterMixin):
    """
    AES crypt filter for public key security handlers.
    """
    pass


class StandardRC4CryptFilter(StandardCryptFilter, RC4CryptFilterMixin):
    """
    RC4 crypt filter for the standard security handler.
    """
    pass


class PubKeyRC4CryptFilter(PubKeyCryptFilter, RC4CryptFilterMixin):
    """
    RC4 crypt filter for public key security handlers.
    """
    pass


STD_CF = generic.NameObject('/StdCF')
"""
Default name to use for the default crypt filter in the standard security
handler.
"""

DEFAULT_CRYPT_FILTER = generic.NameObject('/DefaultCryptFilter')
"""
Default name to use for the default crypt filter in public key security
handlers.
"""

IDENTITY = generic.NameObject('/Identity')
"""
Name of the identity crypt filter.
"""


class CryptFilterConfiguration:
    """
    Crypt filter store attached to a security handler.

    Instances of this class are not designed to be reusable.

    :param crypt_filters:
        A dictionary mapping names to their corresponding crypt filters.
    :param default_stream_filter:
        Name of the default crypt filter to use for streams.
    :param default_stream_filter:
        Name of the default crypt filter to use for strings.
    :param default_file_filter:
        Name of the default crypt filter to use for embedded files.

        .. note::
            PyHanko currently is not aware of embedded files, so managing these
            is the API user's responsibility.
    """

    def __init__(self, crypt_filters: Dict[str, CryptFilter] = None,
                 default_stream_filter=IDENTITY, default_string_filter=IDENTITY,
                 default_file_filter=None):
        def _select(name) -> CryptFilter:
            return (
                IdentityCryptFilter() if name == IDENTITY
                else crypt_filters[name]
            )

        self._crypt_filters = crypt_filters
        self._default_string_filter_name = default_string_filter
        self._default_stream_filter_name = default_stream_filter
        self._default_file_filter_name = default_file_filter
        self._default_stream_filter = _select(default_stream_filter)
        self._default_string_filter = _select(default_string_filter)
        default_file_filter = default_file_filter or default_stream_filter
        self._default_file_filter = _select(default_file_filter)

    def __getitem__(self, item):
        if item == generic.NameObject('/Identity'):
            return IdentityCryptFilter()
        return self._crypt_filters[item]

    def __contains__(self, item):
        return (
            item == generic.NameObject('/Identity')
            or item in self._crypt_filters
        )

    def filters(self):
        """Enumerate all crypt filters in this configuration."""
        return self._crypt_filters.values()

    def set_security_handler(self, handler: 'SecurityHandler'):
        """
        Set the security handler on all crypt filters in this configuration.

        :param handler:
            A :class:`.SecurityHandler` instance.
        """
        for cf in self._crypt_filters.values():
            cf._set_security_handler(handler)

    def get_for_stream(self):
        """
        Retrieve the default crypt filter to use with streams.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_stream_filter

    def get_for_string(self):
        """
        Retrieve the default crypt filter to use with strings.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_string_filter

    def get_for_embedded_file(self):
        """
        Retrieve the default crypt filter to use with embedded files.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_file_filter

    def as_pdf_object(self):
        """
        Serialise this crypt filter configuration to a dictionary object,
        including all its subordinate crypt filters (with the exception of
        the identity filter, if relevant).
        """
        result = generic.DictionaryObject()
        result['/StmF'] = self._default_stream_filter_name
        result['/StrF'] = self._default_string_filter_name
        if self._default_file_filter_name is not None:
            result['/EFF'] = self._default_file_filter_name
        result['/CF'] = generic.DictionaryObject({
            generic.NameObject(key): value.as_pdf_object()
            for key, value in self._crypt_filters.items() if key != IDENTITY
        })
        return result

    def default_filters(self):
        """
        Return the "default" filters associated with this crypt filter
        configuration, i.e. those registered as the defaults for strings
        and streams, respectively.

        These sometimes require special treatment (as per the specification).

        :return:
            A set with one or two elements.
        """
        stmf = self._default_stream_filter
        strf = self._default_string_filter
        return {stmf, strf} if stmf is not strf else {stmf}


def _std_rc4_config(keylen):
    return CryptFilterConfiguration(
        {STD_CF: StandardRC4CryptFilter(keylen=keylen)},
        default_stream_filter=STD_CF,
        default_string_filter=STD_CF
    )


def _pubkey_rc4_config(keylen, recipients=None, encrypt_metadata=True):
    return CryptFilterConfiguration(
        {DEFAULT_CRYPT_FILTER: PubKeyRC4CryptFilter(
            keylen=keylen, acts_as_default=True, recipients=recipients,
            encrypt_metadata=encrypt_metadata
        )},
        default_stream_filter=DEFAULT_CRYPT_FILTER,
        default_string_filter=DEFAULT_CRYPT_FILTER
    )


def _std_aes_config(keylen):
    return CryptFilterConfiguration(
        {STD_CF: StandardAESCryptFilter(keylen=keylen)},
        default_stream_filter=STD_CF,
        default_string_filter=STD_CF
    )


def _pubkey_aes_config(keylen, recipients=None, encrypt_metadata=True):
    return CryptFilterConfiguration(
        {DEFAULT_CRYPT_FILTER: PubKeyAESCryptFilter(
            keylen=keylen, acts_as_default=True, recipients=recipients,
            encrypt_metadata=encrypt_metadata
        )},
        default_stream_filter=DEFAULT_CRYPT_FILTER,
        default_string_filter=DEFAULT_CRYPT_FILTER
    )


@SecurityHandler.register
class StandardSecurityHandler(SecurityHandler):

    @classmethod
    def get_name(cls) -> str:
        return generic.NameObject('/Standard')

    @classmethod
    def build_from_pw_legacy(cls, rev: StandardSecuritySettingsRevision,
                             id1, desired_owner_pass, desired_user_pass=None,
                             keylen_bytes=16, use_aes128=True,
                             crypt_filter_config=None):
        desired_owner_pass = _legacy_normalise_pw(desired_owner_pass)
        desired_user_pass = (
            _legacy_normalise_pw(desired_user_pass)
            if desired_user_pass is not None else desired_owner_pass
        )
        if rev > StandardSecuritySettingsRevision.RC4_OR_AES128:
            raise ValueError(
                f"{rev} is not supported by this bootstrapping method."
            )
        if rev == StandardSecuritySettingsRevision.RC4_BASIC:
            keylen_bytes = 5
        elif use_aes128 and \
                rev == StandardSecuritySettingsRevision.RC4_OR_AES128:
            keylen_bytes = 16
        o_entry = _compute_o_value_legacy(
            desired_owner_pass, desired_user_pass, rev.value, keylen_bytes
        )

        # TODO allow user to set perms, force unavailable perms to 1 if
        #  encrypting with RC4_BASIC
        perms = ALL_PERMS

        if rev == StandardSecuritySettingsRevision.RC4_BASIC:
            u_entry, key = _compute_u_value_r2(
                desired_user_pass, o_entry, perms, id1
            )
        else:
            u_entry, key = _compute_u_value_r34(
                desired_user_pass, rev.value, keylen_bytes, o_entry, perms, id1
            )

        if rev == StandardSecuritySettingsRevision.RC4_OR_AES128:
            version = SecurityHandlerVersion.RC4_OR_AES128
        elif rev == StandardSecuritySettingsRevision.RC4_BASIC:
            version = SecurityHandlerVersion.RC4_40
        else:
            version = SecurityHandlerVersion.RC4_LONGER_KEYS

        if rev == StandardSecuritySettingsRevision.RC4_OR_AES128 and \
                crypt_filter_config is None:
            if use_aes128:
                crypt_filter_config = _std_aes_config(keylen=16)
            else:
                crypt_filter_config = _std_rc4_config(keylen=keylen_bytes)

        sh = StandardSecurityHandler(
            version=version, revision=rev, legacy_keylen=keylen_bytes,
            perm_flags=perms, odata=o_entry,
            udata=u_entry, encrypt_metadata=True,
            crypt_filter_config=crypt_filter_config,
        )
        sh._shared_key = key
        return sh

    @classmethod
    def build_from_pw(cls, desired_owner_pass, desired_user_pass=None):
        owner_pw_bytes = _r6_normalise_pw(desired_owner_pass)
        user_pw_bytes = (
            _r6_normalise_pw(desired_user_pass)
            if desired_user_pass is not None
            else owner_pw_bytes
        )
        encryption_key = secrets.token_bytes(32)
        u_validation_salt = secrets.token_bytes(8)
        u_key_salt = secrets.token_bytes(8)
        u_hash = _r6_hash_algo(user_pw_bytes, u_validation_salt)
        u_entry = u_hash + u_validation_salt + u_key_salt
        u_interm_key = _r6_hash_algo(user_pw_bytes, u_key_salt)
        _, ue_seed = symmetric.aes_cbc_no_padding_encrypt(
            u_interm_key, encryption_key, bytes(16)
        )
        assert len(ue_seed) == 32

        o_validation_salt = secrets.token_bytes(8)
        o_key_salt = secrets.token_bytes(8)
        o_hash = _r6_hash_algo(owner_pw_bytes, o_validation_salt, u_entry)
        o_entry = o_hash + o_validation_salt + o_key_salt
        o_interm_key = _r6_hash_algo(owner_pw_bytes, o_key_salt, u_entry)
        _, oe_seed = symmetric.aes_cbc_no_padding_encrypt(
            o_interm_key, encryption_key, bytes(16)
        )
        assert len(oe_seed) == 32

        # TODO allow user to set perms
        perms_bytes = b'\xfc\xff\xff\xff'
        extd_perms_bytes = (
            perms_bytes + (b'\xff' * 4) + b'Tadb' + secrets.token_bytes(4)
        )
        perms = struct.unpack('<i', perms_bytes)[0]

        # need to encrypt one 16 byte block in CBC mode with an
        # IV of 0 (equivalent to 1 block in ECB mode).
        # FIXME
        #  oscrypto's OpenSSL-based implementation of aes_cbc_no_padding_encrypt
        #  requires padding to encrypt a single 16-byte block if the key size is
        #  larger than 16 bytes. I believe that's a mistake, but since I'm not
        #  willing to vendor oscrypto, this little hack will have to do.

        # We'll indulge in oscrypto's whims and request padding
        _, encrypted_perms = symmetric.aes_cbc_pkcs7_encrypt(
            encryption_key, extd_perms_bytes, bytes(16)
        )

        # ... and then cut the result off at 16 bytes
        # This is OK, because the block size is 16 bytes, so the first 16
        # bytes aren't affected by the rest of the encrypted string
        encrypted_perms = encrypted_perms[:16]
        sh = StandardSecurityHandler(
            version=SecurityHandlerVersion.AES256,
            revision=StandardSecuritySettingsRevision.AES256,
            legacy_keylen=32, perm_flags=perms, odata=o_entry,
            udata=u_entry, oeseed=oe_seed, ueseed=ue_seed,
            encrypted_perms=encrypted_perms, encrypt_metadata=True
        )
        sh._shared_key = encryption_key
        return sh

    def __init__(self, version: SecurityHandlerVersion,
                 revision: StandardSecuritySettingsRevision,
                 legacy_keylen,  # in bytes, not bits
                 perm_flags: int, odata, udata, oeseed=None,
                 ueseed=None, encrypted_perms=None, encrypt_metadata=True,
                 crypt_filter_config: CryptFilterConfiguration = None):
        if crypt_filter_config is None:
            if version == SecurityHandlerVersion.RC4_40:
                crypt_filter_config = _std_rc4_config(5)
            elif version == SecurityHandlerVersion.RC4_LONGER_KEYS:
                crypt_filter_config = _std_rc4_config(legacy_keylen)
            elif version == SecurityHandlerVersion.AES256 \
                    and crypt_filter_config is None:
                # there's a reasonable default config that we can fall back
                # to here
                crypt_filter_config = _std_aes_config(32)
        super().__init__(
            version, legacy_keylen, crypt_filter_config,
            encrypt_metadata=encrypt_metadata
        )
        self.revision = revision
        self.perms = perm_flags
        if revision == StandardSecuritySettingsRevision.AES256:
            if not (len(udata) == len(odata) == 48):
                raise misc.PdfError(
                    "/U and /O entries must be 48 bytes long in a "
                    "rev. 6 security handler"
                )  # pragma: nocover
            if not oeseed or not ueseed or \
                    not (len(oeseed) == len(ueseed) == 32):
                raise misc.PdfError(
                    "/UE and /OE must be present and be 32 bytes long in a "
                    "rev. 6 security handler"
                )  # pragma: nocover
            self.oeseed = oeseed
            self.ueseed = ueseed
            if not encrypted_perms or len(encrypted_perms) != 16:
                raise misc.PdfError(
                    "/Perms must be present and be 16 bytes long in a "
                    "rev. 6 security handler"
                )  # pragma: nocover
            self.encrypted_perms = encrypted_perms
        else:
            if not (len(udata) == len(odata) == 32):
                raise misc.PdfError(
                    "/U and /O entries must be 32 bytes long in a "
                    "legacy security handler"
                )  # pragma: nocover
            self.oeseed = self.ueseed = self.encrypted_perms = None
        self.odata = odata
        self.udata = udata
        self._shared_key = None
        self._auth_failed = False

    @staticmethod
    def read_standard_cf_dictionary(cfdict):
        # TODO does a V4 handler default to /Identity unless the /Encrypt
        #  dictionary specifies a custom filter?
        try:
            cfm = cfdict['/CFM']
        except KeyError:
            return None
        if cfm == '/None':
            return None
        elif cfm == '/V2':
            keylen_bits = cfdict.get('/Length', 40)
            return StandardRC4CryptFilter(keylen=keylen_bits // 8)
        elif cfm == '/AESV2':
            return StandardAESCryptFilter(keylen=16)
        elif cfm == '/AESV3':
            return StandardAESCryptFilter(keylen=32)
        else:
            raise NotImplementedError("No such crypt filter method: " + cfm)

    @classmethod
    def instantiate_from_pdf_object(cls,
                                    encrypt_dict: generic.DictionaryObject):
        v = SecurityHandlerVersion(encrypt_dict['/V'])
        r = StandardSecuritySettingsRevision(encrypt_dict['/R'])
        keylen_bits = encrypt_dict.get('/Length', 40)
        if (keylen_bits % 8) != 0:
            raise misc.PdfError("Key length must be a multiple of 8")
        keylen = keylen_bits // 8
        stmf = encrypt_dict.get('/StmF', IDENTITY)
        strf = encrypt_dict.get('/StrF', IDENTITY)
        eff = encrypt_dict.get('/EFF', stmf)

        try:
            crypt_filters = {
                name: StandardSecurityHandler.read_standard_cf_dictionary(
                    cfdict
                )
                for name, cfdict in encrypt_dict['/CF'].items()
            }
            cfc = CryptFilterConfiguration(
                crypt_filters=crypt_filters, default_stream_filter=stmf,
                default_string_filter=strf, default_file_filter=eff
            )
        except KeyError:
            cfc = None
        return StandardSecurityHandler(
            version=v, revision=r, legacy_keylen=keylen,
            crypt_filter_config=cfc,
            perm_flags=int(encrypt_dict.get('/P', ALL_PERMS)),
            odata=encrypt_dict['/O'].original_bytes[:48],
            udata=encrypt_dict['/U'].original_bytes[:48],
            oeseed=encrypt_dict.get_and_apply(
                '/OE', lambda x: x.original_bytes
            ),
            ueseed=encrypt_dict.get_and_apply(
                '/UE', lambda x: x.original_bytes
            ),
            encrypted_perms=encrypt_dict.get_and_apply(
                '/Perms', lambda x: x.original_bytes
            ),
            encrypt_metadata=encrypt_dict.get_and_apply(
                '/EncryptMetadata', bool, default=True
            )
        )

    def as_pdf_object(self):
        result = generic.DictionaryObject()
        result['/Filter'] = generic.NameObject('/Standard')
        result['/O'] = generic.ByteStringObject(self.odata)
        result['/U'] = generic.ByteStringObject(self.udata)
        result['/P'] = generic.NumberObject(self.perms)
        result['/V'] = generic.NumberObject(self.version.value)
        result['/R'] = generic.NumberObject(self.revision.value)
        # this shouldn't be necessary for V5 handlers, but Adobe Reader
        # requires it anyway ...sigh...
        result['/Length'] = generic.NumberObject(self.keylen * 8)
        if self.version > SecurityHandlerVersion.RC4_LONGER_KEYS:
            result['/EncryptMetadata'] \
                = generic.BooleanObject(self.encrypt_metadata)
            result.update(self.crypt_filter_config.as_pdf_object())
        if self.revision == StandardSecuritySettingsRevision.AES256:
            result['/OE'] = generic.ByteStringObject(self.oeseed)
            result['/UE'] = generic.ByteStringObject(self.ueseed)
            result['/Perms'] = generic.ByteStringObject(self.encrypted_perms)
        return result

    def _auth_user_password_legacy(self, id1: bytes, password):
        rev = self.revision
        user_token = self.udata
        if rev == StandardSecuritySettingsRevision.RC4_BASIC:
            user_tok_supplied, key = _compute_u_value_r2(
                password, self.odata, self.perms, id1
            )
        else:
            user_tok_supplied, key = _compute_u_value_r34(
                password, rev.value, self.keylen, self.odata, self.perms, id1
            )
            user_tok_supplied = user_tok_supplied[:16]
            user_token = user_token[:16]

        return user_tok_supplied == user_token, key

    def _authenticate_legacy(self, id1: bytes, password):
        user_password, key = self._auth_user_password_legacy(id1, password)
        if user_password:
            return AuthResult.USER, key
        else:
            rev = self.revision
            key = _compute_o_value_legacy_prep(password, rev.value, self.keylen)
            if rev == StandardSecuritySettingsRevision.RC4_BASIC:
                userpass = symmetric.rc4_encrypt(key, self.odata)
            else:
                val = self.odata
                for i in range(19, -1, -1):
                    new_key = bytes(b ^ i for b in key)
                    val = symmetric.rc4_encrypt(new_key, val)
                userpass = val
            owner_password, key = self._auth_user_password_legacy(id1, userpass)
            if owner_password:
                return AuthResult.OWNER, key
        return AuthResult.FAILED, None

    def authenticate(self, credential, id1: bytes = None):
        rev = self.revision
        if rev == StandardSecuritySettingsRevision.AES256:
            res, key = self._authenticate_r6(credential)
        else:
            if id1 is None:
                raise ValueError(
                    "id1 must be specified for legacy encryption"
                )  # pragma: nocover
            credential = _legacy_normalise_pw(credential)
            res, key = self._authenticate_legacy(id1, credential)
        if key is not None:
            self._shared_key = key
        else:
            self._auth_failed = True
        return res

    # Algorithm 2.A in ISO 32000-2 § 7.6.4.3.3
    def _authenticate_r6(self, password) -> Tuple[AuthResult, Optional[bytes]]:
        pw_bytes = _r6_normalise_pw(password)
        o_entry_split = _R6KeyEntry.from_bytes(self.odata)
        u_entry_split = _R6KeyEntry.from_bytes(self.udata)

        if _r6_password_authenticate(pw_bytes, o_entry_split, self.udata):
            result = AuthResult.OWNER
            key = _r6_derive_file_key(
                pw_bytes, o_entry_split, self.oeseed, self.udata
            )
        elif _r6_password_authenticate(pw_bytes, u_entry_split):
            result = AuthResult.USER
            key = _r6_derive_file_key(pw_bytes, u_entry_split, self.ueseed)
        else:
            return AuthResult.FAILED, None

        # check the file key against the perms entry

        # Standard says ECB (which oscrypto doesn't support),
        # but one round of CBC with IV = 0 is equivalent to ECB
        decrypted_p_entry = symmetric.aes_cbc_no_padding_decrypt(
            key, self.encrypted_perms, bytes(16)
        )

        # known plaintext mandated in the standard ...sigh...
        perms_ok = decrypted_p_entry[9:12] == b'adb'
        perms_ok &= self.perms == struct.unpack('<i', decrypted_p_entry[:4])[0]
        try:
            # check encrypt_metadata flag
            decr_metadata_flag = _EXPECTED_PERMS_8[decrypted_p_entry[8]]
            perms_ok &= decr_metadata_flag == self.encrypt_metadata
        except KeyError:
            perms_ok = False

        if not perms_ok:
            raise misc.PdfError(
                "File decryption key didn't decrypt permission flags "
                "correctly -- file permissions may have been tampered with."
            )
        return result, key

    def get_file_encryption_key(self) -> bytes:
        key = self._shared_key
        if key is None:
            raise misc.PdfReadError(
                "Authentication failed." if self._auth_failed
                else "No key available to decrypt, please authenticate first."
            )
        return key


@enum.unique
class PubKeyAdbeSubFilter(enum.Enum):
    S3 = generic.NameObject('/adbe.pkcs7.s3')
    S4 = generic.NameObject('/adbe.pkcs7.s4')
    S5 = generic.NameObject('/adbe.pkcs7.s5')


def construct_envelope_content(seed: bytes, perms: int,
                               include_permissions=True):
    assert len(seed) == 20
    return seed + (struct.pack('<i', perms) if include_permissions else b'')


def _recipient_info(envelope_key: bytes, cert: x509.Certificate):
    pubkey = cert.public_key
    pubkey_algo_info: PublicKeyAlgorithm = pubkey['algorithm']
    algorithm_name = pubkey_algo_info['algorithm'].native
    if algorithm_name != 'rsa':
        raise NotImplementedError(
            f"Certificate public key must be of type 'rsa', "
            f"not '{algorithm_name}'."
        )

    assert len(envelope_key) == 32

    # TODO having support for oeap here would be cool, but as with PSS
    #  oscrypto only supports the default parameters.
    encrypted_data = asymmetric.rsa_pkcs1v15_encrypt(
        asymmetric.load_public_key(pubkey), envelope_key
    )
    # TODO support subjectKeyIdentifier here (requiring version 2)
    rid = cms.RecipientIdentifier({
        'issuer_and_serial_number': cms.IssuerAndSerialNumber({
            'issuer': cert.issuer, 'serial_number': cert.serial_number
        })
    })
    algo = cms.KeyEncryptionAlgorithm({
        'algorithm': cms.KeyEncryptionAlgorithmId('rsaes_pkcs1v15')
    })
    return cms.RecipientInfo({
        'ktri': cms.KeyTransRecipientInfo({
            'version': 0, 'rid': rid, 'key_encryption_algorithm': algo,
            'encrypted_key': encrypted_data
        })
    })


def construct_recipient_cms(certificates: List[x509.Certificate], seed: bytes,
                            perms: int, include_permissions=True) \
        -> cms.ContentInfo:

    # The content of the generated ContentInfo object
    # is an object of type EnvelopedData, containing a 20 byte seed (+ perms).
    #
    # This seed is shared among all recipients (including those occurring in
    # other CMS objects, if relevant), and is the only secret part of the
    # key derivation procedure used to obtain the file encryption key.
    #
    # The envelope content is then encrypted using an envelope key,
    # which is in turn encrypted using the public key of each recipient and
    # stored in a RecipientInfo object (more precisely, a
    # KeyTransRecipientInfo object). PyHanko always uses AES-256 to encrypt
    # the envelope content, even if the chosen PDF encryption is weaker.
    #
    # The RecipientInfo objects, algorithm specification and envelope content
    # are then bundled into an EnvelopedData object.
    envelope_content = construct_envelope_content(
        seed, perms, include_permissions=include_permissions
    )
    # 256-bit key used to encrypt the envelope
    envelope_key = secrets.token_bytes(32)
    # encrypt the envelope content with the envelope key
    iv, encrypted_envelope_content = symmetric.aes_cbc_pkcs7_encrypt(
        envelope_key, envelope_content, iv=None
    )

    # encrypt the envelope key for each recipient
    rec_infos = [_recipient_info(envelope_key, cert) for cert in certificates]

    algo = cms.EncryptionAlgorithm({
        'algorithm': EncryptionAlgorithmId('aes256_cbc'),
        'parameters': iv
    })
    encrypted_content_info = cms.EncryptedContentInfo({
        'content_type': cms.ContentType('data'),
        'content_encryption_algorithm': algo,
        'encrypted_content': encrypted_envelope_content
    })

    # version 0 because no originatorInfo, no attribute certs
    # and all recipientinfo structures have version 0 (and aren't' pwri)
    enveloped_data = cms.EnvelopedData({
        'version': 0, 'recipient_infos': rec_infos,
        'encrypted_content_info': encrypted_content_info
    })

    # finally, package up the whole thing into a ContentInfo object
    return cms.ContentInfo({
        'content_type': cms.ContentType('enveloped_data'),
        'content': enveloped_data
    })


# TODO implement a PKCS#11 version of this interface
class EnvelopeKeyDecrypter:
    def __init__(self, cert: x509.Certificate):
        self.cert = cert

    def decrypt(self, encrypted_key: bytes,
                algo_params: cms.KeyEncryptionAlgorithm) -> bytes:
        raise NotImplementedError


class SimpleEnvelopeKeyDecrypter(EnvelopeKeyDecrypter):
    def __init__(self, cert: x509.Certificate, private_key: PrivateKeyInfo):
        super().__init__(cert)
        self.private_key = private_key

    @staticmethod
    def load(key_file, cert_file, key_passphrase=None):
        try:
            # load cryptographic data (both PEM and DER are supported)
            with open(key_file, 'rb') as f:
                private_key: PrivateKeyInfo = oskeys.parse_private(
                    f.read(), password=key_passphrase
                )
            with open(cert_file, 'rb') as f:
                cert: x509.Certificate = oskeys.parse_certificate(
                    f.read()
                )
        except (IOError, ValueError) as e:  # pragma: nocover
            logger.error('Could not load cryptographic material', e)
            return None
        return SimpleEnvelopeKeyDecrypter(
            cert=cert, private_key=asymmetric.load_private_key(private_key)
        )

    @classmethod
    def load_pkcs12(cls, pfx_file, passphrase=None):

        try:
            with open(pfx_file, 'rb') as f:
                pfx_bytes = f.read()
        except IOError as e:  # pragma: nocover
            logger.error(f'Could not open PKCS#12 file {pfx_file}.', e)
            return None

        (kinfo, cert, other_certs) = oskeys.parse_pkcs12(pfx_bytes, passphrase)

        return SimpleEnvelopeKeyDecrypter(
            cert=cert, private_key=asymmetric.load_private_key(kinfo)
        )

    def decrypt(self, encrypted_key: bytes,
                algo_params: cms.KeyEncryptionAlgorithm) -> bytes:
        algo_name = algo_params['algorithm'].native
        if algo_name != 'rsaes_pkcs1v15':
            raise NotImplementedError(
                f"Only 'rsaes_pkcs1v15' is supported for envelope encryption, "
                f"not '{algo_name}'."
            )
        return asymmetric.rsa_pkcs1v15_decrypt(
            self.private_key, encrypted_key
        )


def read_seed_from_recipient_cms(recipient_cms: cms.ContentInfo,
                                 decrypter: EnvelopeKeyDecrypter):
    content_type = recipient_cms['content_type'].native
    if content_type != 'enveloped_data':
        raise misc.PdfReadError(
            "Recipient CMS content type must be enveloped data, not "
            + content_type
        )  # pragma: nocover
    ed: cms.EnvelopedData = recipient_cms['content']
    encrypted_content_info = ed['encrypted_content_info']
    rec_info: cms.RecipientInfo
    for rec_info in ed['recipient_infos']:
        ktri = rec_info.chosen
        if not isinstance(ktri, cms.KeyTransRecipientInfo):
            raise misc.PdfReadError(
                "RecipientInfo must be of type KeyTransRecipientInfo."
            )  # pragma: nocover
        issuer_and_serial = ktri['rid'].chosen
        if not isinstance(issuer_and_serial, cms.IssuerAndSerialNumber):
            raise NotImplementedError(
                "Recipient identifier must be of type IssuerAndSerialNumber."
            )
        issuer = issuer_and_serial['issuer']
        serial = issuer_and_serial['serial_number'].native
        if decrypter.cert.issuer == issuer and \
                decrypter.cert.serial_number == serial:
            # we have a match!
            # use the decrypter passed in to decrypt the envelope key
            # for this recipient.
            envelope_key = decrypter.decrypt(
                ktri['encrypted_key'].native,
                ktri['key_encryption_algorithm']
            )
            break
    else:
        return None

    # we have the envelope key
    # next up: decrypting the envelope

    algo: cms.EncryptionAlgorithm = \
        encrypted_content_info['content_encryption_algorithm']
    encrypted_envelope_content = \
        encrypted_content_info['encrypted_content'].native

    # the spec says that we have to support rc4 (<=256 bits),
    # des, triple des, rc2 (<=128 bits)
    # and AES-CBC (128, 192, 256 bits)
    cipher_name = algo.encryption_cipher
    with_iv = {
        'aes': symmetric.aes_cbc_pkcs7_decrypt,
        'des': symmetric.des_cbc_pkcs5_decrypt,
        'tripledes': symmetric.tripledes_cbc_pkcs5_decrypt,
        'rc2': symmetric.rc2_cbc_pkcs5_decrypt
    }

    if cipher_name in with_iv:
        decryption_fun = with_iv[cipher_name]
        iv = algo.encryption_iv
        content = decryption_fun(envelope_key, encrypted_envelope_content, iv)
    elif cipher_name == 'rc4':
        content = symmetric.rc4_encrypt(
            envelope_key, encrypted_envelope_content
        )
    else:
        raise misc.PdfReadError(
            f"Cipher {cipher_name} is not allowed in PDF."
        )

    return content[:20]


@SecurityHandler.register
class PubKeySecurityHandler(SecurityHandler):

    @staticmethod
    def build_from_certs(certs: List[x509.Certificate],
                         keylen_bytes=16,
                         version=SecurityHandlerVersion.AES256,
                         use_aes=True, use_crypt_filters=True,
                         encrypt_metadata=True) -> 'PubKeySecurityHandler':
        subfilter = (
            PubKeyAdbeSubFilter.S5 if use_crypt_filters
            else PubKeyAdbeSubFilter.S4
        )
        cfc = None
        if version == SecurityHandlerVersion.RC4_OR_AES128:
            # only in this case we need a CFC, otherwise the constructor
            # takes care of it
            if use_aes:
                cfc = _pubkey_aes_config(
                    16, encrypt_metadata=encrypt_metadata,
                    recipients=None
                )
            else:
                cfc = _pubkey_rc4_config(
                    keylen_bytes, recipients=None,
                    encrypt_metadata=encrypt_metadata
                )
        sh = PubKeySecurityHandler(
            version, subfilter, keylen_bytes,
            encrypt_metadata=encrypt_metadata, crypt_filter_config=cfc,
            recipient_objs=None
        )
        sh.add_recipients(certs)
        return sh

    def __init__(self, version: SecurityHandlerVersion,
                 pubkey_handler_subfilter: PubKeyAdbeSubFilter,
                 legacy_keylen, encrypt_metadata=True,
                 crypt_filter_config: 'CryptFilterConfiguration' = None,
                 recipient_objs: list = None):

        # I don't see how it would be possible to handle V4 without
        # crypt filters in an unambiguous way. V5 should be possible in
        # principle, but Adobe Reader rejects that combination, so meh.
        if version >= SecurityHandlerVersion.RC4_OR_AES128 and \
                pubkey_handler_subfilter != PubKeyAdbeSubFilter.S5:
            raise misc.PdfError(
                "Subfilter /adbe.pkcs7.s5 is required for security handlers "
                "beyond V4."
            )

        if crypt_filter_config is None:
            if version == SecurityHandlerVersion.RC4_40:
                crypt_filter_config = _pubkey_rc4_config(
                    keylen=5, encrypt_metadata=encrypt_metadata,
                    recipients=recipient_objs
                )
            elif version == SecurityHandlerVersion.RC4_LONGER_KEYS:
                crypt_filter_config = _pubkey_rc4_config(
                    keylen=legacy_keylen, encrypt_metadata=encrypt_metadata,
                    recipients=recipient_objs
                )
            elif version == SecurityHandlerVersion.AES256:
                # there's a reasonable default config that we can fall back to
                # here
                crypt_filter_config = _pubkey_aes_config(
                    keylen=32, encrypt_metadata=encrypt_metadata,
                    recipients=recipient_objs
                )
        super().__init__(
            version, legacy_keylen, crypt_filter_config,
            encrypt_metadata=encrypt_metadata,
        )
        self.subfilter = pubkey_handler_subfilter
        self.encrypt_metadata = encrypt_metadata
        self._shared_key = None

    @classmethod
    def get_name(cls) -> str:
        return generic.NameObject('/Adobe.PubSec')

    @classmethod
    def support_generic_subfilters(cls) -> Set[str]:
        return {x.value for x in PubKeyAdbeSubFilter}

    @classmethod
    def instantiate_from_pdf_object(cls,
                                    encrypt_dict: generic.DictionaryObject):
        v = SecurityHandlerVersion(encrypt_dict['/V'])
        try:
            subfilter = misc.get_and_apply(
                encrypt_dict, '/SubFilter', PubKeyAdbeSubFilter, default=(
                    PubKeyAdbeSubFilter.S5 if '/CF' in encrypt_dict
                    else PubKeyAdbeSubFilter.S4
                )
            )
        except ValueError:
            raise misc.PdfReadError(
                "Invalid /SubFilter in public key encryption dictionary: "
                + encrypt_dict['/SubFilter']
            )

        keylen_bits = encrypt_dict.get('/Length', 128)
        if (keylen_bits % 8) != 0:
            raise misc.PdfError("Key length must be a multiple of 8")
        keylen = keylen_bits // 8
        try:
            stmf = encrypt_dict.get('/StmF', IDENTITY)
            strf = encrypt_dict.get('/StrF', IDENTITY)
            eff = encrypt_dict.get('/EFF', stmf)
            default_filters = {stmf, strf}
            crypt_filters = {
                name: PubKeySecurityHandler.read_pubkey_cf_dictionary(
                    cfdict, name in default_filters
                )
                for name, cfdict in encrypt_dict['/CF'].items()
            }
            if subfilter != PubKeyAdbeSubFilter.S5:
                raise misc.PdfReadError(
                    "Crypt filters require /adbe.pkcs7.s5 as the declared "
                    "handler."
                )

            cfc = CryptFilterConfiguration(
                crypt_filters=crypt_filters, default_stream_filter=stmf,
                default_string_filter=strf, default_file_filter=eff
            )
        except KeyError:
            if subfilter == PubKeyAdbeSubFilter.S5:
                raise misc.PdfReadError(
                    "/adbe.pkcs7.s5 handler requires crypt filters."
                )
            cfc = None
        recipients = misc.get_and_apply(
            encrypt_dict, '/Recipients',
            lambda lst: [cms.ContentInfo.load(x.original_bytes) for x in lst]
        )
        return PubKeySecurityHandler(
            version=v, pubkey_handler_subfilter=subfilter,
            legacy_keylen=keylen, recipient_objs=recipients,
            crypt_filter_config=cfc,
            encrypt_metadata=encrypt_dict.get_and_apply(
                '/EncryptMetadata', bool, default=True
            )
        )

    def as_pdf_object(self):
        result = generic.DictionaryObject()
        result['/Filter'] = generic.NameObject(self.get_name())
        result['/SubFilter'] = self.subfilter.value
        result['/V'] = generic.NumberObject(self.version.value)
        result['/Length'] = generic.NumberObject(self.keylen * 8)
        if self.version > SecurityHandlerVersion.RC4_LONGER_KEYS:
            result['/EncryptMetadata'] \
                = generic.BooleanObject(self.encrypt_metadata)
        if self.subfilter == PubKeyAdbeSubFilter.S5:
            # include crypt filter config
            result.update(self.crypt_filter_config.as_pdf_object())
        else:
            # load recipients from default crypt filter into the encryption dict
            default_cf = self.get_stream_filter()
            if not isinstance(default_cf, PubKeyCryptFilter):
                raise TypeError  # pragma: nocover
            result['/Recipients'] = generic.ArrayObject(
                generic.ByteStringObject(recp.dump())
                for recp in default_cf.recipients
            )
        return result

    def add_recipients(self, certs: List[x509.Certificate]):
        # add recipients to all *default* crypt filters
        # callers that want to do this more granularly are welcome to, but
        # then they have to do the legwork themselves.

        for cf in self.crypt_filter_config.default_filters():
            if not isinstance(cf, PubKeyCryptFilter):
                continue
            cf.add_recipients(certs)

    def authenticate(self, credential: EnvelopeKeyDecrypter, id1=None) \
            -> AuthResult:
        if not isinstance(credential, EnvelopeKeyDecrypter):
            raise misc.PdfReadError(
                f"Pubkey authentication credential must be an instance of "
                f"EnvelopeKeyDecrypter, not {type(credential)}."
            )  # pragma: nocover

        for cf in self.crypt_filter_config.default_filters():
            if not isinstance(cf, PubKeyCryptFilter):
                continue
            recp: cms.ContentInfo
            result = cf.authenticate(credential)
            if result == AuthResult.FAILED:
                return AuthResult.FAILED
        return AuthResult.USER

    @staticmethod
    def read_pubkey_cf_dictionary(cfdict, acts_as_default):
        try:
            cfm = cfdict['/CFM']
            recipients = cfdict['/Recipients']
        except KeyError:
            raise misc.PdfReadError(
                "PubKey CF dictionary must have /Recipients and /CFM keys"
            )
        if isinstance(recipients, generic.ByteStringObject):
            recipients = recipients,
        recipient_objs = [
            cms.ContentInfo.load(x.original_bytes) for x in recipients
        ]
        encrypt_metadata = cfdict.get('/EncryptMetadata', True)
        if cfm == '/None':
            raise misc.PdfReadError(
                "/None doesn't make sense in a PubKey CF dictionary"
            )
        elif cfm == '/V2':
            keylen_bits = cfdict.get('/Length', 40)
            return PubKeyRC4CryptFilter(
                keylen=keylen_bits // 8,
                encrypt_metadata=encrypt_metadata, recipients=recipient_objs,
                acts_as_default=acts_as_default
            )
        elif cfm == '/AESV2':
            return PubKeyAESCryptFilter(
                keylen=16,
                encrypt_metadata=encrypt_metadata, recipients=recipient_objs,
                acts_as_default=acts_as_default
            )
        elif cfm == '/AESV3':
            return PubKeyAESCryptFilter(
                keylen=32,
                encrypt_metadata=encrypt_metadata, recipients=recipient_objs,
                acts_as_default=acts_as_default
            )
        else:
            raise NotImplementedError("No such crypt filter method: " + cfm)
