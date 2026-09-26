# Network presence ingest

Active VPS source for receiving authenticated device-presence updates and maintaining the notifier path into Home Assistant/Telegram. Configure the bearer token and Home Assistant/Telegram credentials in local environment files; none are included. The service unit is a portable example that must be adjusted to the target host and data directory.

The included notifier uses configured secrets at runtime. The private presence database and live network/device identifiers are excluded.
