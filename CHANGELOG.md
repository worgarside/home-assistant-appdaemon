# CHANGELOG

<!-- version list -->

## v0.28.0 (2026-08-02)

### Bug Fixes

- Handle missing credentials gracefully
  ([#278](https://github.com/worgarside/home-assistant-appdaemon/pull/278),
  [`ddd38ce`](https://github.com/worgarside/home-assistant-appdaemon/commit/ddd38ce47898bd722d2a3677be4d5de130803b3f))

### Features

- Add streak to habit notifications
  ([#280](https://github.com/worgarside/home-assistant-appdaemon/pull/280),
  [`6276218`](https://github.com/worgarside/home-assistant-appdaemon/commit/62762189d530ae63cc9bb05e20925934a747eca4))

- Implement OAuth broker for improved authorization
  ([#279](https://github.com/worgarside/home-assistant-appdaemon/pull/279),
  [`13612b8`](https://github.com/worgarside/home-assistant-appdaemon/commit/13612b8ff6ceb110a18266beb41c0e0697ecca66))


## v0.27.1 (2026-08-01)

### Bug Fixes

- Secure token storage for persistence
  ([#277](https://github.com/worgarside/home-assistant-appdaemon/pull/277),
  [`63bf576`](https://github.com/worgarside/home-assistant-appdaemon/commit/63bf5766e6a0ca78d482ba1267263f97dc0e8c9d))


## v0.27.0 (2026-07-30)

### Features

- Add habit type count sensors
  ([#272](https://github.com/worgarside/home-assistant-appdaemon/pull/272),
  [`40b354f`](https://github.com/worgarside/home-assistant-appdaemon/commit/40b354f70d700ecd216d9f6a638916dcc35ed9ce))

- **truelayer**: Introduce support for balance retrieval with variants
  ([#273](https://github.com/worgarside/home-assistant-appdaemon/pull/273),
  [`9f2ac24`](https://github.com/worgarside/home-assistant-appdaemon/commit/9f2ac24715dcfed87f5ddeab4d0cb9217d82d79b))


## v0.26.0 (2026-07-28)

### Refactoring

- Update job scheduling trigger to 'immediate' for all apps
  ([#271](https://github.com/worgarside/home-assistant-appdaemon/pull/271),
  [`d05f18c`](https://github.com/worgarside/home-assistant-appdaemon/commit/d05f18ce022958da85504843a924f2b75c8803b5))


## v0.25.0 (2026-07-28)

### Bug Fixes

- Correct "as of" date parsing to address inaccurate data display
  ([#270](https://github.com/worgarside/home-assistant-appdaemon/pull/270),
  [`8dfaaed`](https://github.com/worgarside/home-assistant-appdaemon/commit/8dfaaed31f5c30a0abe68b8ee39b50bf9bc1bee1))

### Features

- Add reauthorization handling for Monzo
  ([#269](https://github.com/worgarside/home-assistant-appdaemon/pull/269),
  [`4ea1e03`](https://github.com/worgarside/home-assistant-appdaemon/commit/4ea1e03a7eec156f008096fdc1583fe9d4ca3fa1))

- Add SLC timestamp sensors
  ([#268](https://github.com/worgarside/home-assistant-appdaemon/pull/268),
  [`2095c91`](https://github.com/worgarside/home-assistant-appdaemon/commit/2095c916d180e003a45640dd7e358e39acb5ffd1))

- **cursor**: Add Cursor usage monitoring and MQTT integration
  ([#266](https://github.com/worgarside/home-assistant-appdaemon/pull/266),
  [`fc4b99e`](https://github.com/worgarside/home-assistant-appdaemon/commit/fc4b99e7fd8a5ac194326299700da31dd7d845b5))

- **habit**: Add habit and mood tracking to Home Assistant
  ([#267](https://github.com/worgarside/home-assistant-appdaemon/pull/267),
  [`f1ca3cc`](https://github.com/worgarside/home-assistant-appdaemon/commit/f1ca3ccc0b9bda87b2b1ea4718b837dfce4a64cc))


## v0.24.1 (2026-07-26)

### Bug Fixes

- Improve polling logic for AC app
  ([#265](https://github.com/worgarside/home-assistant-appdaemon/pull/265),
  [`5bdec67`](https://github.com/worgarside/home-assistant-appdaemon/commit/5bdec67d134a5cf11a1f2053b4431e03e4faf92b))


## v0.24.0 (2026-07-22)

### Features

- Add SLC MQTT sensors ([#264](https://github.com/worgarside/home-assistant-appdaemon/pull/264),
  [`9c2dffa`](https://github.com/worgarside/home-assistant-appdaemon/commit/9c2dffa17d4cf10a830645077c612e9221177740))


## v0.23.0 (2026-07-17)

### Features

- Support automatic reauth for TrueLayer
  ([#261](https://github.com/worgarside/home-assistant-appdaemon/pull/261),
  [`6ba6eb1`](https://github.com/worgarside/home-assistant-appdaemon/commit/6ba6eb1433302da93ea0097c16d133f8db995e9b))


## v0.22.1 (2026-06-28)

### Bug Fixes

- Improve availability management
  ([#258](https://github.com/worgarside/home-assistant-appdaemon/pull/258),
  [`1a7a8c5`](https://github.com/worgarside/home-assistant-appdaemon/commit/1a7a8c509c9e8b9ff7716c0d169cb2a58d1c90e5))


## v0.22.0 (2026-06-27)

### Features

- Add Pro Breeze AC support
  ([#257](https://github.com/worgarside/home-assistant-appdaemon/pull/257),
  [`c533d16`](https://github.com/worgarside/home-assistant-appdaemon/commit/c533d1612f7e48c78c884e99bdc3ab048b14e858))

### Refactoring

- Overhaul AppDaemon tooling and type checks
  ([#256](https://github.com/worgarside/home-assistant-appdaemon/pull/256),
  [`fafef57`](https://github.com/worgarside/home-assistant-appdaemon/commit/fafef57128df772f175a995fe5af8c5268c4b4a4))

- **ci**: Replace poetry with uv and update dependency management
  ([`bca41fd`](https://github.com/worgarside/home-assistant-appdaemon/commit/bca41fd1562f918e69881d70652bd7389241b7ae))


## v0.21.1 (2025-11-18)


## v0.21.0 (2025-10-20)


## v0.20.0 (2025-07-30)

### Features

- Disable Cosmo app ([#229](https://github.com/worgarside/home-assistant-appdaemon/pull/229),
  [`1f9c7fa`](https://github.com/worgarside/home-assistant-appdaemon/commit/1f9c7fa12c90d89e74d3dbc2014283823e04e591))


## v0.19.0 (2025-03-15)


## v0.18.0 (2024-10-03)


## v0.17.2 (2024-09-19)


## v0.17.1 (2024-08-22)


## v0.17.0 (2024-08-21)


## v0.16.2 (2024-06-27)


## v0.16.1 (2024-06-25)


## v0.16.0 (2024-06-24)


## v0.15.0 (2024-05-26)


## v0.14.0 (2024-05-25)


## v0.13.0 (2024-05-18)


## v0.12.0 (2024-05-17)


## v0.11.0 (2024-05-16)


## v0.10.2 (2024-05-11)


## v0.10.1 (2024-03-03)


## v0.10.0 (2024-01-22)


## v0.9.9 (2024-01-10)


## v0.9.8 (2024-01-07)


## v0.9.7 (2024-01-06)


## v0.9.6 (2024-01-05)


## v0.9.5 (2024-01-02)


## v0.9.4 (2024-01-01)


## v0.9.3 (2024-01-01)


## v0.9.2 (2023-12-31)


## v0.9.1 (2023-12-31)


## v0.9.0 (2023-12-31)


## v0.8.6 (2023-12-09)


## v0.8.5 (2023-12-09)


## v0.8.4 (2023-12-08)


## v0.8.3 (2023-12-03)


## v0.8.2 (2023-12-02)


## v0.8.1 (2023-12-02)


## v0.8.0 (2023-12-02)


## v0.7.5 (2023-12-02)


## v0.7.4 (2023-12-01)


## v0.7.3 (2023-12-01)


## v0.7.2 (2023-11-29)


## v0.7.1 (2023-11-22)


## v0.7.0 (2023-11-15)


## v0.6.0 (2023-11-15)


## v0.5.0 (2023-11-11)


## v0.4.4 (2023-11-10)


## v0.4.3 (2023-11-05)


## v0.4.2 (2023-11-05)


## v0.4.1 (2023-11-05)


## v0.4.0 (2023-11-05)


## v0.3.1 (2023-11-01)


## v0.3.0 (2023-11-01)


## v0.2.0 (2023-10-26)


## v0.1.0 (2023-10-26)


## v0.0.1 (2023-10-22)

- Initial Release
