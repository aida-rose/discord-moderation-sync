# Security Policy

## Supported Versions

This project is currently maintained on the `main` branch.

Security fixes will be applied to the latest version of the code in this repository.

## Reporting a Vulnerability

Please do **not** publicly disclose security vulnerabilities by opening a public GitHub issue.

If you find a security issue, please contact the maintainer privately:

**Contact Info:**

Email: aidarose.pb@gmail.com

Discord: aida_rose


When reporting a vulnerability, please include:

- A clear description of the issue
- Steps to reproduce the issue, if possible
- The affected file or feature
- The possible impact
- Any suggested fix, if you have one

Please do **not** include private user data, Discord tokens, database files, `.env` files, logs, CSV files, or server invite links in your report.

## Sensitive Data

This repository should never contain:

- Discord bot tokens
- Discord client secrets
- `.env` files
- Database files such as `.sqlite3`, `.db`, or `.sqlite`
- Runtime CSV files
- User IDs from real moderation data
- Minecraft usernames from real users
- Private Discord invite links
- Moderation logs
- Server-specific private configuration

Use `.env.example` for placeholder configuration values.

## Bot Token Safety

If a Discord bot token is accidentally committed or exposed:

1. Immediately reset the token in the Discord Developer Portal.
2. Remove the token from the repository.
3. Remove the token from Git history if necessary.
4. Restart the bot with the new token.
5. Review recent bot activity for suspicious behavior.

## Responsible Disclosure

Please give the maintainer reasonable time to review and fix security issues before discussing them publicly.

Security reports made in good faith are appreciated.
