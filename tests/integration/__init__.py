"""Integration tests: the platform against something that speaks AWS.

Everything in ``tests/unit`` runs against the local lake backend. These tests run
against a *real* wire protocol — either moto (in-process AWS mocks, which need no
Docker and are what CI uses) or Floci (the local emulator, which is what a developer
runs).

The distinction matters because the unit suite structurally cannot catch a
mis-signed request, a wrong endpoint, a bucket policy that denies the very role it
was written for, or a paginator that only returns the first page. Those failures
live at the boundary, so at least some tests have to live there too.
"""
