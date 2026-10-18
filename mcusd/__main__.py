"""Allow ``python -m mcusd`` as an alias for ``mc-usd-export``."""

from mcusd.cli import main

raise SystemExit(main())
