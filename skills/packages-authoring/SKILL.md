---
name: packages-authoring
description: Author, import, and update Freeside OS package manifests and package.justfile recipes.
---

# Freeside Package Authoring

Use this skill when creating, converting, or updating packages under `packages/<pkg_name>/`.

## Mandatory Conventions
- Use `just_pkg_create`, `just_pkg_convert`, and `just_pkg_checksum` instead of manual boilerplate or raw shell downloads.
- Recipes in `package.justfile` must dynamically reference `$PKG_NAME`, `$PKG_VERSION`, `$PKG_GROUP`, and `$PKG_DEPENDENCIES` exported by Straylight. Never hardcode version strings inside `build` or `package` targets.
- Always install artifacts relative to `$DESTDIR` in the `package` target.
- Place compiler and build environment flags (`CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `CONFIGURE_ARGS`, `MAKE_FLAGS`) under `[build.environment]` in `package.manifest`.
- Validate changes with `just_pkg_verify` and `just_pkg_resolve` before running `just_pkg_build`.

