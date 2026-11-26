# Sunny Bot
## A lightweight XML -> discord /command converter

Hi! Do you want a discord bot that can pat your friends from a list of pat gifs without needing to add a suspicious third party bot to your server? Sunny bot does that!

The command.xml here controls the commands for the bot - Basically everything is set in the xml. The bot itself is extremely lightweight - essentially only a few async loops (and checking the repo for update every 5 minutes. Please respect rate limits!) - and only needs a discord developer application and an active connection to the internet.

To host this bot for yourself, you can fork the repo and change commands.xml to your liking. As for the hardware, you can use your pc (if you only need the bot to run when your pc is on!) or set up a VPS. To install discord.py (the only dependency), you can run:
```
pip install discord.py
```

When you're ready, you can run the bot by setting the environment variables in a python venv, or as an systemd service via a .service file. An example .service file might look like:

```
# Change COMMANDS_URL to match your own repo.
# Copy to /etc/systemd/system/sunny-bot.service, then:
# sudo systemctl daemon-reload && sudo systemctl enable --now sunny-bot

[Unit]
Description=Sunny bot - converts XML into discord slash commands
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<Change this!>
WorkingDirectory=<Change this!>
Environment=DISCORD_TOKEN=<Change this!>
Environment=COMMANDS_URL=https://raw.githubusercontent.com/duckofdisorder/sunny-bot/main/commands.xml
# Environment=GITHUB_TOKEN=only-needed-for-a-private-repo
# Environment=GUILD_ID=<Set if using for a private server!>
ExecStart=/usr/bin/python3 <Your working directory goes here!>/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

The application you create via the dev portal needs application.commands perms (and bot perms!) The bot only needs permissions to view channels, send messages and embed links. Crucially, it cannot snoop on your messages.