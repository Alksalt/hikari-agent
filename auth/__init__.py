"""auth — token storage and OAuth provider abstractions.

Public surface:
  from auth.store import default_store, MemoryStore, KeychainStore
  from auth.providers import get_provider, load_scope_config, reload_scope_config
  from auth.google import GoogleProvider
"""
