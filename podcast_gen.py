import os
import datetime
import yt_dlp
from github import Github
from feedgen.feed import FeedGenerator

# --- CONFIGURATION DE VOS FLUX ---
# Pour chaque playlist, choisissez un nom de fichier (ex: tech.xml) et collez l'URL
PLAYLISTS_CONFIG = [
    {
        "filename": "42.xml",
        "url": "https://youtube.com/playlist?list=PLCwXWOyIR22s1vddGJB3NNRSI45jEy_sE"
    },
    {
        "filename": "squeezie_horreur.xml",
        "url": "https://youtube.com/playlist?list=PLTYUE9O6WCrjvZmJp2fXTWOgypGvByxMv"
    },
    # Vous pouvez copier-coller ce bloc {} pour ajouter d'autres playlists
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

    # Chargement de l'historique global pour éviter les doublons
    downloaded_ids = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            downloaded_ids = f.read().splitlines()

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(id)s.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}],
        'quiet': True,
        'ignoreerrors': True
    }

    # Boucle sur chaque configuration de playlist
    for item in PLAYLISTS_CONFIG:
        rss_filename = item['filename']
        playlist_url = item['url']
        
        print(f"\n--- Traitement du flux : {rss_filename} ---")

        # Préparation du générateur RSS
        fg = FeedGenerator()
        fg.load_extension('podcast')
        
        # Si le fichier XML existe déjà, on le charge pour garder l'historique
        if os.path.exists(rss_filename):
            try: fg.parse_file(rss_filename)
            except: pass
        else:
            # Sinon on crée les métadonnées de base
            fg.title(f'Podcast {rss_filename}')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            fg.description('Généré automatiquement via GitHub Actions')

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Récupération des infos de la playlist
            info = ydl.extract_info(playlist_url, download=False)
            
            if not info or 'entries' not in info:
                print(f"Playlist inaccessible ou vide : {playlist_url}")
                continue

            # On met à jour le titre du podcast avec le vrai titre de la Playlist YouTube
            current_title = info.get('title', f'Podcast {rss_filename}')
            fg.title(current_title)

            for entry in info['entries']:
                if not entry: continue
                vid_id = entry['id']

                # Si la vidéo est déjà dans l'historique global, on saute
                if vid_id in downloaded_ids:
                    continue

                print(f"Nouveau téléchargement : {entry['title']}")

                try:
                    # 1. Télécharger le MP3
                    ydl.download([entry['webpage_url']])
                    mp3_filename = f"{vid_id}.mp3"
                    
                    # 2. Envoyer vers GitHub Releases (Stockage)
                    print("Upload vers GitHub Releases...")
                    
                    # Vérification si le fichier existe déjà dans la Release (cas rare)
                    asset_exists = False
                    for asset in release.get_assets():
                        if asset.name == mp3_filename:
                            asset_exists = True
                            download_url = asset.browser_download_url
                            break
                    
                    if not asset_exists:
                        asset = release.upload_asset(mp3_filename)
                        download_url = asset.browser_download_url

                    # 3. Ajouter l'épisode au RSS
                    fe = fg.add_entry()
                    fe.id(vid_id)
                    fe.title(entry['title'])
                    fe.description(entry.get('description', 'Pas de description'))
                    fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                    # C'est ici que se crée le lien magique pour l'appli de podcast
                    fe.enclosure(download_url, 0, 'audio/mpeg')

                    # 4. Mettre à jour le Log local
                    with open(LOG_FILE, "a") as log:
                        log.write(f"{vid_id}\n")
                        downloaded_ids.append(vid_id)
                    
                    # Supprimer le fichier mp3 du serveur de traitement (le runner)
                    if os.path.exists(mp3_filename):
                        os.remove(mp3_filename)

                except Exception as e:
                    print(f"Erreur sur {vid_id}: {e}")

        # Sauvegarde du fichier XML spécifique à cette playlist
        fg.rss_file(rss_filename)
        print(f"Fichier {rss_filename} mis à jour.")

if __name__ == "__main__":
    run()
