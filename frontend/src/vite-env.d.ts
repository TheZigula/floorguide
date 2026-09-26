// vite-env.d.ts — the type declarations for Vite's build-time environment.
// Owns: the shape of import.meta.env, so VITE_API_BASE is a typed string and not "any".

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the FastAPI backend, e.g. http://localhost:8000. Empty means: use the mock. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
