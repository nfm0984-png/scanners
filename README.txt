NEW REPO SETUP (does not touch your existing scanners)

1. github.com -> New repository -> name it e.g. "scanners" -> Public -> Create.
2. Add file -> Upload files -> upload: index.html, whale.html, veterans.html,
   veterans-data.json, veterans_scan.py  -> Commit.
3. Add file -> Create new file -> name: .github/workflows/veterans.yml
   -> paste the contents of veterans.yml -> Commit.
4. Settings -> Pages -> Source: Deploy from a branch -> main / root -> Save.
5. (Kalshi page only) Settings -> Secrets and variables -> Actions -> New secret:
   APIFY_TOKEN. Then Actions -> veterans-scan -> Run workflow once.
   Edit REPO = "nfm0984-png/kalshi-scanner" near the top of veterans.html's
   script to your NEW repo name (owner/repo).
6. Whale page: open it, Settings tab, paste your NEW Orca key. Done.
