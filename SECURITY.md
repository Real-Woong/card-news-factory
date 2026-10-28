# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security tab) instead of a public issue.

## Handling secrets

- Secrets live only in `.env` (git-ignored). `.env.example` contains placeholders only.
- Do not paste tokens into issues, logs or pull requests.
- The code never prints token values; `python -m src.core.envcheck` reports only names and status.

## Automatic posting

Posting to a real account is **off by default** and needs several explicit switches
(`instagram.channel.phase`, `instagram.channel.publish`, `instagram.publishing.auto_publish`) plus
credentials. Before turning it on:

- Run it for a while in review mode and read the candidates yourself.
- Keep the pause file (`logs/PAUSE_PUBLISHING`) in mind: while it exists nothing is posted.
- Keep the daily cap (`max_posts_per_day`) low.
- Follow the platform's terms of service and your local rules on AI-generated content.

The approval gates reduce the chance of publishing a wrong claim. They do not guarantee that a
claim is true: they check that text matches the collected evidence, and the evidence itself can be wrong.
