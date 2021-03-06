***************
Release history
***************


0.2.0
=====

*Release date:* 2021-01-10

New features and enhancements
-----------------------------

Signing
^^^^^^^

* Allow the caller to specify an output stream when signing.

Validation
^^^^^^^^^^

* The incremental update analysis functionality has been heavily refactored
  into something more rule-based and modular. The new difference analysis system
  is also much more user-configurable, and a (sufficiently motivated) library
  user could even plug in their own implementation.
* The new validation system treats ``/Metadata`` updates more correctly, and fixes
  a number of other minor stability problems.
* Improved validation logging and status reporting mechanisms.
* Improved seed value constraint enforcement support: this includes added
  support for  ``/V``, ``/MDP``, ``/LockDocument``, ``/KeyUsage``
  and (passive) support for ``/AppearanceFilter`` and  ``/LegalAttestation``.

CLI
^^^

* You can now specify negative page numbers on the command line to refer to the
  pages of a document in reverse order.

General PDF API
^^^^^^^^^^^^^^^

* Added convenience functions to retrieve references from dictionaries and
  arrays.
* Tweaked handling of object freeing operations; these now produce PDF ``null``
  objects instead of (Python) ``None``.


Bugs fixed
----------

* ``root_ref`` now consistently returns a ``Reference`` object
* Corrected wrong usage of ``@freeze_time`` in tests that caused some failures
  due to certificate expiry issues.
* Fixed a gnarly caching bug in ``HistoricalResolver`` that sometimes leaked
  state from later revisions into older ones.
* Prevented cross-reference stream updates from accidentally being saved with
  the same settings as their predecessor in the file. This was a problem when
  updating files generated by other PDF processing software.


0.1.0
=====

*Release date:* 2020-12-30

Initial release.