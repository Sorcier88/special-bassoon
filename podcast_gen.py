import os
import datetime
import yt_dlp
import time
import random
from github import Github
from feedgen.feed import FeedGenerator

# --- CONFIGURATION (Remettez vos playlists ici) ---
PLAYLISTS_CONFIG = [
    {
        "filename": "42.xml",
        "url": "https://youtube.com/playlist?list=PLCwXWOyIR22s1vddGJB3NNRSI45jEy_sE"
    },
    {
        "filename": "squeezie_horreur.xml",
        "url": "https://youtube.com/playlist?list=PLTYUE9O6WCrjvZmJp2fXTWOgypGvByxMv"
    },
]

# --- NE RIEN TOUCHER EN DESSOUS ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
LOG_FILE = "downloaded_log.txt"

def get_or_create_release(repo):
    try:
        return repo.get_release(RELEASE_TAG)
    except:
        return repo.create_git_release(tag=RELEASE_TAG, name="Audio Files", message="Stockage MP3", draft=False, prerelease=False)

def run():
    g = Github(os.environ['GITHUB_TOKEN'])
    repo = g.get_repo(REPO_NAME)
    release = get_or_create_release(repo)

    downloaded_ids = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            downloaded_ids = f.read().splitlines()

    # --- CONFIGURATION RENFORCÉE ---
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(id)s.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}],
        'quiet': False, # On veut voir les logs
        'ignoreerrors': True, # IMPORTANT: Continue même si erreur 429 ou vidéo privée
        'no_warnings': True,
        # L'astuce magique : Se faire passer pour un client Android
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
        # Pause aléatoire entre 10 et 30 secondes pour ne pas énerver YouTube
        'sleep_interval': 10,
        'max_sleep_interval': 30
    }

    for item in PLAYLISTS_CONFIG:
        rss_filename = item['filename']
        playlist_url = item['url']
        
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
                info = ydl.extract_info(playlist_url, download=False)
                
                if not info or 'entries' not in info:
                    print("Erreur lecture playlist (429 possible). On réessaiera demain.")
                    continue

                fg.title(info.get('title', f'Podcast {rss_filename}'))

                for entry in info['entries']:
                    if not entry: continue # Vidéo privée ou supprimée renvoie None
                    vid_id = entry['id']

                    if vid_id in downloaded_ids:
                        continue

                    print(f"Téléchargement : {entry['title']}")

                    try:
                        ydl.download([entry['webpage_url']])
                        mp3_filename = f"{vid_id}.mp3"
                        
                        # Vérif si le fichier est bien là (en cas d'erreur silencieuse)
                        if not os.path.exists(mp3_filename):
                            print("Échec téléchargement fichier.")
                            continue

                        # Upload GitHub
                        asset_exists = False
                        for asset in release.get_assets():
                            if asset.name == mp3_filename:
                                asset_exists = True
                                download_url = asset.browser_download_url
                                break
                        
                        if not asset_exists:
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
                        
                        # Petite pause de sécurité supplémentaire
                        time.sleep(random.randint(5, 10))

                    except Exception as e:
                        print(f"Erreur sur {vid_id}: {e}")

        except Exception as eGlobal:
            print(f"Erreur critique sur la playlist : {eGlobal}")

        fg.rss_file(rss_filename)
        print(f"Sauvegarde {rss_filename}")

if __name__ == "__main__":
    run()
