import os
import json
import datetime
import yt_dlp
import time
import random
from github import Github
from feedgen.feed import FeedGenerator

# --- CONSTANTES ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
LOG_FILE = "downloaded_log.txt"
CONFIG_FILE = "playlists.json"

def get_or_create_release(repo):
    try:
        return repo.get_release(RELEASE_TAG)
    except:
        return repo.create_git_release(tag=RELEASE_TAG, name="Audio Files", message="Stockage MP3", draft=False, prerelease=False)

def run():
    # 1. Chargement de la configuration
    if not os.path.exists(CONFIG_FILE):
        print(f"Erreur : Le fichier {CONFIG_FILE} est introuvable.")
        return

    with open(CONFIG_FILE, 'r') as f:
        try:
            playlists_config = json.load(f)
        except json.JSONDecodeError:
            print(f"Erreur : Le fichier {CONFIG_FILE} est mal formé (Erreur de syntaxe JSON).")
            return

    # 2. Connexion GitHub
    try:
        g = Github(os.environ['GITHUB_TOKEN'])
        repo = g.get_repo(REPO_NAME)
        release = get_or_create_release(repo)
    except Exception as e:
        print(f"Erreur connexion GitHub: {e}")
        return

    # 3. Chargement Historique
    downloaded_ids = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            downloaded_ids = f.read().splitlines()

    # 4. Configuration YT-DLP (Renforcée Anti-Bot)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(id)s.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}],
        'quiet': False,
        'ignoreerrors': True, # Continue même si erreur
        'no_warnings': True,
        'force_ipv4': True,   # Force IPv4 (souvent moins bloqué par Google)
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web_embedded'], # Masque le script en iPhone ou Web
            }
        },
        'sleep_interval': 15,    # Pause min
        'max_sleep_interval': 40 # Pause max
    }

    # 5. Boucle sur les playlists
    for item in playlists_config:
        rss_filename = item.get('filename')
        playlist_url = item.get('url')
        
        if not rss_filename or not playlist_url:
            print("Configuration invalide pour une entrée (filename ou url manquant).")
            continue

        print(f"\n--- Traitement : {rss_filename} ---")

        fg = FeedGenerator()
        fg.load_extension('podcast')
        
        # Charger RSS existant
        if os.path.exists(rss_filename):
            try: fg.parse_file(rss_filename)
            except: pass
        else:
            fg.title(f'Podcast {rss_filename}')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            fg.description('Auto-generated Podcast')

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Tentative d'extraction des infos
                try:
                    info = ydl.extract_info(playlist_url, download=False)
                except Exception as e:
                    print(f"Erreur d'accès à la playlist (Blocage possible): {e}")
                    # On sauvegarde le RSS tel quel pour ne pas le perdre
                    fg.rss_file(rss_filename)
                    continue

                if not info or 'entries' not in info:
                    print("Playlist vide ou illisible.")
                    continue

                fg.title(info.get('title', f'Podcast {rss_filename}'))

                for entry in info['entries']:
                    if not entry: continue
                    vid_id = entry['id']

                    if vid_id in downloaded_ids:
                        continue

                    print(f"Nouveau : {entry.get('title', vid_id)}")

                    try:
                        # Téléchargement
                        ydl.download([entry['webpage_url']])
                        mp3_filename = f"{vid_id}.mp3"
                        
                        if not os.path.exists(mp3_filename):
                            print("Fichier non téléchargé (vidéo privée ou bloquée).")
                            continue

                        # Upload
                        asset_exists = False
                        for asset in release.get_assets():
                            if asset.name == mp3_filename:
                                asset_exists = True
                                download_url = asset.browser_download_url
                                break
                        
                        if not asset_exists:
                            print("Upload vers GitHub...")
                            asset = release.upload_asset(mp3_filename)
                            download_url = asset.browser_download_url

                        # Mise à jour RSS
                        fe = fg.add_entry()
                        fe.id(vid_id)
                        fe.title(entry['title'])
                        fe.description(entry.get('description', '-'))
                        fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                        fe.enclosure(download_url, 0, 'audio/mpeg')

                        # Log
                        with open(LOG_FILE, "a") as log:
                            log.write(f"{vid_id}\n")
                            downloaded_ids.append(vid_id)
                        
                        # Nettoyage & Pause
                        os.remove(mp3_filename)
                        time.sleep(random.randint(10, 20))

                    except Exception as e:
                        print(f"Erreur téléchargement {vid_id}: {e}")

        except Exception as eGlobal:
            print(f"Erreur critique playlist : {eGlobal}")

        fg.rss_file(rss_filename)
        print(f"Sauvegarde {rss_filename}")

if __name__ == "__main__":
    run()
