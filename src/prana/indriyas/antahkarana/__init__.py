"""Antahkarana (अन्तःकरण) — the inner instrument.

Cognition glue between sense and action. Classically four faculties:

  - manas    — low-level association / integration
  - buddhi   — discriminative judgment
  - citta    — memory store
  - ahankara — the I-sense / continuity of self

In this project these are not separate modules; they are distributed
across the existing cognition stack:

  manas    ≈ heartbeat daemon (prana.heartbeat)
  buddhi   ≈ viveka LoRA       (prana.heartbeat.viveka)
  citta    ≈ smriti memory tree (~/.narada, smriti package)
  ahankara ≈ identity files in ~/.narada/

This package exists for namespace completeness and for future modules
that don't fit cleanly elsewhere.
"""
