===============================================
foundata.utils Ansible collection Release Notes
===============================================

.. contents:: Topics

v1.0.1
======

Bugfixes
--------

- http_client - ``HttpClient.request()`` and ``decode_json()`` now annotate their ``body`` as ``bytes | bytearray``. Both already accepted a bytearray at run time, so a product can pass a large request body without a copy and without a cast for its type checker.

v1.0.0
======

Release Summary
---------------

First release of ``foundata.utils``, the shared module utilities of foundata's product collections: ``errors``, ``deadline``, ``http_client`` and ``exec``, and the ``foundata.utils.http_client`` and ``foundata.utils.exec`` documentation fragments.
