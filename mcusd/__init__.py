"""Minecraft-USD-Exporter: export Minecraft Java Edition worlds to OpenUSD stages."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("minecraft-usd-exporter")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
