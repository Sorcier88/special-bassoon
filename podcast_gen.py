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
COOKIE_FILE = "cookies.txt" # Fichier temporaire

def get_or_create_release(repo):
    try:
        return repo.get_release(RELEASE_TAG)
    except:
        return repo.create_git_release(tag=RELEASE_TAG, name="Audio Files", message="Stockage MP3", draft=False, prerelease=False)

def run():
    # 0. Création du fichier cookies depuis le Secret
    cookies_env = os.environ.get('YOUTUBE_COOKIES')
    if cookies_env:
        print("Cookies trouvés, création du fichier d'authentification...")
        with open(COOKIE_FILE, 'w') as f:
            f.write(cookies_env)
    else:
        print("ATTENTION: Pas de cookies trouvés dans les Secrets. Risque d'échec élevé.")

    # 1. Chargement Config
    if not os.path.exists(CONFIG_FILE):
        return

    with open(CONFIG_FILE, 'r') as f:
        try:
            playlists_config = json.load(f)
        except:
            return

    # 2. Connexion GitHub
    try:
        g = Github(os.environ['GITHUB_TOKEN'])
        repo = g.get_repo(REPO_NAME)
        release = get_or_create_release(repo)
    except Exception as e:
        print(f"Erreur GitHub: {e}")
        return

    # 3. Chargement Historique
    downloaded_ids = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            downloaded_ids = f.read().splitlines()

    # 4. Configuration YT-DLP (Mode Authentifié)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(id)s.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}],
        'quiet': False,
        'ignoreerrors': True,
        'no_warnings': True,
        'sleep_interval': 5,
        'max_sleep_interval': 15
    }

    # Si le fichier cookie a été créé, on l'ajoute aux options
    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    # 5. Boucle principale
    for item in playlists_config:
        rss_filename = item.get('filename')
        playlist_url = item.get('url')
        
        if not rss_filename or not playlist_url: continue

        print(f"\n--- Traitement : {rss_filename} ---")
        fg = FeedGenerator()
        fg.load_extension('podcast')
        
        if os.path.exists(rss_filename):
            try: fg.parse_file(rss_filename)
            except: pass
        else:
            fg.title(f'Podcast {rss_filename}')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            fg.description('Auto-generated')

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(playlist_url, download=False)
                except Exception as e:
                    print(f"Erreur playlist (Cookies invalides ou autre): {e}")
                    fg.rss_file(rss_filename)
                    continue

                if not info or 'entries' not in info: continue

                fg.title(info.get('title', f'Podcast {rss_filename}'))

                for entry in info['entries']:
                    if not entry: continue
                    vid_id = entry['id']

                    if vid_id in downloaded_ids: continue

                    print(f"Nouveau : {entry.get('title', vid_id)}")

                    try:
                        ydl.download([entry['webpage_url']])
                        mp3_filename = f"{vid_id}.mp3"
                        
                        if not os.path.exists(mp3_filename):
                            print("Echec téléchargement.")
                            continue

                        asset_exists = False
                        for asset in release.get_assets():
                            if asset.name == mp3_filename:
                                asset_exists = True
                                download_url = asset.browser_download_url
                                break
                        
                        if not asset_exists:
                            print("Upload...")
                            asset = release.upload_asset(mp3_filename)
                            download_url = asset.browser_download_url

                        fe = fg.add_entry()
                        fe.id(vid_id)
                        fe.title(entry['title'])
                        fe.description(entry.get('description', '-'))
                        fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                        fe.enclosure(download_url, 0, 'audio/mpeg')

                        with open(LOG_FILE, "a") as log:
                            log.write(f"{vid_id}\n")
                            downloaded_ids.append(vid_id)
                        
                        os.remove(mp3_filename)

                    except Exception as e:
                        print(f"Erreur téléchargement {vid_id}: {e}")

        except Exception as eGlobal:
            print(f"Erreur critique : {eGlobal}")

        fg.rss_file(rss_filename)
        print(f"Sauvegarde {rss_filename}")

    # NETTOYAGE CRITIQUE : Suppression du fichier cookies
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)
        print("Cookies temporaires supprimés.")

if __name__ == "__main__":
    run()
