# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0](https://github.com/acme/acme-python/compare/v1.2.4...v2.0.0) (2026-09-30)

### ⚠ BREAKING CHANGES

* **api:** `Client.charges.list()` now requires `tenant`
* drop support for Python 3.8 and 3.9

### Features

* **pets:** add `upload_photo()` ([#120](https://github.com/acme/acme-python/issues/120))

## [1.2.4](https://github.com/acme/acme-python/compare/v1.2.3...v1.2.4) (2026-09-10)

### Bug Fixes

* **client:** retry on 429 ([#118](https://github.com/acme/acme-python/issues/118))

## [1.2.3](https://github.com/acme/acme-python/compare/v1.2.2...v1.2.3) (2026-08-01)

### Bug Fixes

* **types:** widen `Pet.weight_kg` to float
