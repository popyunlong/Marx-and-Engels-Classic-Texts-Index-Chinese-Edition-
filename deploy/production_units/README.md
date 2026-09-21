# Production-only units

This directory contains only production-specific services that have no canonical peer in `deploy/`.

Shared services and timers, including the single authoritative `marx-search.service`, live directly in `deploy/`. Do not copy a shared unit into this directory; update its canonical file instead.
