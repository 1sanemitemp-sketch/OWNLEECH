# Leech Bot (WZML-X style, no torrent)

Commands:
- /leech <link> [-z] [-e] [-n new name] [-t thumb_link]   (alias: /l)
- /status      - running tasks + CPU/RAM/disk
- /usetting    - upload type, thumbnail, prefix, suffix
- /cancel_<id> - task cancel
- /a ID  |  /ua ID  - (owner only) user/group authorize ba unauthorize. Reply kore ba group e shudhu /a dileo hoy

Args:
-z zip | -e extract | -n new name | -t thumbnail link

## Railway deploy
1. GitHub e ei folder push koro (ba Railway CLI diye deploy).
2. Railway -> New Project -> Deploy from GitHub repo (Dockerfile auto detect hobe).
3. Variables e dao: API_ID, API_HASH, BOT_TOKEN, OWNER_ID (+ optional AUTHORIZED_CHATS, MAX_CONCURRENT).
4. Service e Volume attach koro, Mount Path: `/data` (ekbar korlei hobe). Bot nijei /data use kore, kono variable lagbe na. Redeploy/restart e settings ar thumbnail thakbe.

API_ID/API_HASH: my.telegram.org  |  BOT_TOKEN: @BotFather
