"""``security/`` — OS security subsystem group (#1682 #8 narrow).

Groups the runtime security packages under one domain dir to cut top-level
over-flat-ness: ``permissions/`` (permission resolution), ``secrets/`` (secret
store/loader/oauth/interpolation), ``sandbox/`` (exec sandboxing backends).

NOT included: ``safe/`` (FP-0042 allowlist, ``reyn.api.safe.`` literal-gated,
owner-gated) and ``limits/`` (reliability/runtime-limits, not security).
"""
