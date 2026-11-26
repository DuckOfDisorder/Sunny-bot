import asyncio
import hashlib
import logging
import os
import random
import re
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN =         os.environ.get("DISCORD_TOKEN")
BASE_DIR =      os.path.dirname(os.path.abspath(__file__))
LOG_PATH =      os.environ.get("LOG_FILE", os.path.join(BASE_DIR, "bot.log"))
XML_PATH =      os.environ.get("COMMANDS_XML", os.path.join(BASE_DIR, "commands.xml"))
GUILD_ID =      os.environ.get("GUILD_ID")
COMMANDS_URL =  os.environ.get("COMMANDS_URL", "").strip()
GITHUB_TOKEN =  os.environ.get("GITHUB_TOKEN", "").strip()
RELOAD_MINUTES = float(os.environ.get("RELOAD_MINUTES", "5"))

# refuse anything bigger than 1 MB
MAX_XML_BYTES = 1024 * 1024  

# Discord's rule for slash-command names
NAME_RE = re.compile(r"^[-_a-z0-9]{1,32}$")

log = logging.getLogger("bot")

def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )

    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)


def parse_commands(raw):
    # Parse XML bytes 
    # {name: {"links", "verb", "description", "solo"}}. 
    root = ET.fromstring(raw)
    commands = {}
    for el in root.iter("command"):
        name = (el.get("name") or "").strip().lower()
        
        if not NAME_RE.match(name):
            log.warning("Skipping invalid command name: %r", name)
            continue
        links = []

        for l in el.iter("link"):
            url = (l.text or "").strip()
            
            if url.startswith(("https://", "http://")):
                links.append(url)
            elif url:
                log.warning("/%s: skipping non-http link %r", name, url)
        
        if not links:
            log.warning("Skipping /%s: no valid links", name)
            continue

        verb = (el.get("verb") or name).strip()[:100]
        commands[name] = {
            "links": links,
            "verb": discord.utils.escape_mentions(verb),
            "description": (el.get("description") or name).strip()[:100],
            "solo": (el.get("solo") or "").strip().lower() in ("true", "yes", "1"),
        }
    return commands


def save_cache(raw):
    # Write the last good XML to disk.
    tmp = XML_PATH + ".tmp"
    
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, XML_PATH)
    
    except OSError as e:
        log.warning("Could not save cached copy to %s: %s", XML_PATH, e)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)
        self.tree.error(self.on_app_command_error)
        self.guild = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
        self.http_session = None
        self.etag = None
        self.commands_data = {}
        self.xml_hash = None
        self.synced_signature = None
        self.stale_checked = False

    # command handling

    async def respond(self, interaction, name, target=None):
        data = self.commands_data.get(name)
        if not data:
            await interaction.response.send_message(
                "That command is no longer available.", ephemeral=True
            )
            return
        link = random.choice(data["links"])
        embed = discord.Embed(color=discord.Color.blurple())
        embed.set_image(url=link)

        text = f"{interaction.user.mention} {data['verb']}"
        ping = [interaction.user]
        
        if target is not None:
            text += f" {target.mention}"
            ping.append(target)

        await interaction.response.send_message(
            content=text,
            embed=embed,
            
            # Only ping these users
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, replied_user=False, users=ping
            ),
        )
        log.info(
            "/%s by %s (%s) -> %s in guild=%s channel=%s link=%s",
            name, interaction.user, interaction.user.id,
            f"{target} ({target.id})" if target else "(solo)",
            interaction.guild_id, interaction.channel_id, link,
        )

    def make_callback(self, name, solo):
        if solo:
            async def callback(interaction: discord.Interaction):
                await self.respond(interaction, name)
        
        else:
            @app_commands.describe(user="Who to target")
            async def callback(interaction: discord.Interaction, user: discord.User):
                await self.respond(interaction, name, user)
        return callback

    async def on_app_command_error(self, interaction, error):
        cmd = interaction.command.name if interaction.command else "?"
        log.error("Error in /%s: %s", cmd, error, exc_info=error)
        
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong.", ephemeral=True)
        
        except discord.HTTPException:
            pass

    # XML loading / syncing

    async def fetch_remote(self):
        # Download the XML. Returns bytes, or None if unchanged since last fetch (HTTP 304).
        headers = {"User-Agent": "discord-xml-command-bot"}
        
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        
        if self.etag:
            headers["If-None-Match"] = self.etag
        
        async with self.http_session.get(COMMANDS_URL, headers=headers) as resp:
            if resp.status == 304:
                return None
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await resp.content.read(MAX_XML_BYTES + 1)
            if len(raw) > MAX_XML_BYTES:
                raise RuntimeError(f"file is larger than {MAX_XML_BYTES} bytes")
            self.etag = resp.headers.get("ETag")
            return raw

    async def read_source(self):
        # Return (xml_bytes or None if nothing new, comes from remote).
        if COMMANDS_URL:
            try:
                return await self.fetch_remote(), True
            
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                log.error("Fetching %s failed: %s", COMMANDS_URL, e)
                if self.commands_data:
                    return None, False  # keep running with what we have
                log.warning("No commands loaded yet; trying cached copy %s", XML_PATH)
        
        try:
            with open(XML_PATH, "rb") as f:
                return f.read(), False
        
        except OSError as e:
            log.error("Cannot read %s: %s", XML_PATH, e)
            return None, False

    async def reload_commands(self):
        raw, from_remote = await self.read_source()

        if raw is not None:
            digest = hashlib.sha256(raw).hexdigest()
            if digest != self.xml_hash:
                self.xml_hash = digest  
                # set even on failure so a bad file is logged once
                source = COMMANDS_URL if from_remote else XML_PATH
                
                try:
                    data = parse_commands(raw)
                
                except ET.ParseError as e:
                    log.error("XML parse error in %s: %s (keeping current)", source, e)
                    data = None
                
                if data:
                    self.commands_data = data
                    log.info("Loaded %d command(s) from %s", len(data), source)
                    if from_remote:
                        save_cache(raw)  # only good XML ever replaces the cache
                
                elif data is not None:
                    log.warning("No valid commands in %s (keeping current)", source)

        # Only talk to Discord when changed.
        # Link and verb changes take effect immediately.
        signature = sorted(
            (n, d["description"], d["solo"]) for n, d in self.commands_data.items()
        )
        
        if signature and signature != self.synced_signature:
            self.tree.clear_commands(guild=self.guild)
            
            for name, d in self.commands_data.items():
                self.tree.add_command(
                    app_commands.Command(
                        name=name,
                        description=d["description"],
                        callback=self.make_callback(name, d["solo"]),
                    ),
                    guild=self.guild,
                )
            
            try:
                synced = await self.tree.sync(guild=self.guild)
                self.synced_signature = signature
                log.info("Synced %d command(s): %s", len(synced),
                         ", ".join("/" + c.name for c in synced))
            
            except discord.HTTPException as e:
                log.error("Command sync failed: %s", e)

    # background loop
    @tasks.loop(minutes=RELOAD_MINUTES)
    async def reload_loop(self):
        try:
            await self.reload_commands()
        
        except Exception:
            log.exception("Unexpected error while reloading commands")

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        log.info("Command source: %s", COMMANDS_URL or XML_PATH)
        await self.reload_commands()
        
        if not self.commands_data:
            log.warning("Starting with no commands; will keep checking")
        self.reload_loop.start()
    
    # Remove old commands from Discord
    async def remove_stale_commands(self):
        if self.guild:
            scopes = [None]  # we use one server -> clear old global commands
        
        else:
            scopes = list(self.guilds)  # we use global -> clear old per-server commands
        
        for scope in scopes:
            where = "global" if scope is None else f"server {scope.id}"
            try:
                stale = await self.tree.fetch_commands(guild=scope)
                if not stale:
                    continue
                await self.tree.sync(guild=scope)
                log.info("Removed %d old %s command(s): %s", len(stale), where,
                         ", ".join("/" + c.name for c in stale))
                
            except discord.HTTPException as e:
                log.warning("Could not clean up old %s commands: %s", where, e)

    async def on_ready(self):
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        # on_ready also fires on reconnects; only do this once
        if not self.stale_checked:
            self.stale_checked = True
            await self.remove_stale_commands()

    async def close(self):
        self.reload_loop.cancel()
        
        if self.http_session:
            await self.http_session.close()
        await super().close()


if __name__ == "__main__":
    setup_logging()
    
    if not TOKEN:
        log.critical("DISCORD_TOKEN environment variable is not set")
        raise SystemExit(1)
    
    if COMMANDS_URL and not COMMANDS_URL.startswith("https://"):
        log.critical("COMMANDS_URL must start with https://")
        raise SystemExit(1)
    Bot().run(TOKEN, log_handler=None)