"""Two-tenant negative tests for the reads scoped in Phase 1c step 2.

Every test here seeds both tenants and then asserts an absence: that tenant
A's answer does not contain, count or sum anything belonging to tenant B.
Asserting only that A can see its own rows would pass just as happily against
the unscoped queries these replace, which is why the assertions are written
this way round.

The default tenant already holds rows from the rest of the suite, so its
figures are compared against a baseline taken inside the same test rather than
against a constant. The second tenant is created empty, so its figures are
exact.

Cleanup is