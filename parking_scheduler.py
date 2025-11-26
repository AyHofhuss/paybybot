import os
import sys
import yaml
import json
from datetime import datetime, timedelta, timezone
import time
import subprocess
import pytz

# --- Configuration des Limites ---
MAX_WAIT_SECONDS = 14400  # 4 heures
MARGIN_SECONDS = 2700     # 45 minutes pour la relance
# Heure de fin de stationnement à Paris (20:00 heure locale)
# Pendant le job, l'heure courante (UTC) est utilisée. 
# 20h00 CET (hiver, UTC+1) = 19h00 UTC
# 20h00 CEST (été, UTC+2) = 18h00 UTC
PARIS_END_PARKING_HOUR_LOCAL = 20

CONFIG_PATH = os.environ.get('CONFIG_FILE', './paybybot3.yml')
CONFIG_NAME = os.environ.get('CONFIG_ACCOUNT_NAME')
GH_TOKEN = os.environ.get('GH_PAT')
REPO_SLUG = os.environ.get('GITHUB_REPOSITORY')


def get_paris_end_of_parking_utc(today_utc: datetime) -> datetime:
    """Calcule le timestamp de fin de stationnement (20h00 Paris) pour la date du jour, en UTC."""
    
    paris_tz = pytz.timezone('Europe/Paris')
    
    # 1. Obtenir la date du jour à partir de l'heure UTC du runner (pour déterminer "aujourd'hui")
    today_date_in_paris = today_utc.astimezone(paris_tz).date()
    
    # 2. Créer l'objet datetime à 20h00 Paris (heure locale)
    paris_end_naive = datetime(
        today_date_in_paris.year, 
        today_date_in_paris.month, 
        today_date_in_paris.day, 
        PARIS_END_PARKING_HOUR_LOCAL, 0, 0
    )
    
    # 3. Localiser (appliquer le fuseau horaire de Paris)
    paris_end_local = paris_tz.localize(paris_end_naive)
    
    # 4. Convertir en UTC pour la comparaison
    return paris_end_local.astimezone(timezone.utc)


def inject_secrets():
    """Charge le YAML et injecte tous les secrets."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Erreur: Fichier de configuration non trouvé à {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    # Récupération des secrets pour injection
    pbp_plate = os.environ['PBP_PLATE']
    pbp_login = os.environ['PAYBYPHONE_LOGIN']
    pbp_pass = os.environ['PAYBYPHONE_PASS']
    pbp_payment_id = os.environ['PBP_PAYMENT_ID']
    #notif_user = os.environ['NOTIF_USER']
    #notif_pass = os.environ['NOTIF_PASS']
    
    # Injection des valeurs dans la configuration (en supposant la structure 'example_account')
    account = config.get(CONFIG_NAME, {})
    
    if not account:
        print(f"Erreur: Compte '{CONFIG_NAME}' non trouvé dans le YAML.", file=sys.stderr)
        sys.exit(1)

    account['plate'] = pbp_plate
    account['paybyphone']['login'] = pbp_login
    account['paybyphone']['password'] = pbp_pass
    account['paymentAccountId'] = pbp_payment_id
  

    # Écriture du fichier mis à jour
    with open(CONFIG_PATH, 'w') as f:
        yaml.safe_dump(config, f, sort_keys=False)

    print("Configuration YAML mise à jour et secrets injectés.")


def execute_payment_and_analyze():
    """Exécute paybybot3, analyse la sortie et prend une décision."""
    
    # Les arguments paybybot3
    args = [
        sys.executable, "-m", "paybybot3", "pay", CONFIG_NAME,
        "--config", CONFIG_PATH,
        "--location", os.environ['PBP_LOCATION'],
        "--rate", os.environ['PBP_RATE'],
        "--duration", os.environ['PBP_DURATION'],
        "--unit", os.environ['PBP_UNIT'],
    ]
  
    # ----------------------------------------------------
    # 1. PREMIÈRE TENTATIVE DE PAIEMENT
    # ----------------------------------------------------
    process = subprocess.run(args, capture_output=True, text=True)
    log_output = process.stdout + process.stderr
    
    print("--- Log d'exécution paybybot3 ---")
    print(log_output)
    print("-----------------------------------")
    
    # Gestion des erreurs techniques
    if process.returncode != 0 and "Already registered" not in log_output:
        print("Erreur: Le paiement a échoué et la session n'est pas en cours.", file=sys.stderr)
        sys.exit(process.returncode)

    # Gestion du succès immédiat
    if "Already registered" not in log_output:
        print("Paiement réussi. Fin du job.")
        sys.exit(0)

    # ----------------------------------------------------
    # 2. ANALYSE DE L'EXPIRATION (Car session en cours)
    # ----------------------------------------------------
    try:
        # Recherche regex robuste pour la date
        import re
        m = re.search(r"'expireTime': datetime.datetime\((\d{4}, \d{1,2}, \d{1,2}, \d{1,2}, \d{1,2}, \d{1,2})\)", log_output)
        
        if not m:
             print("Avertissement: Session en cours, mais expireTime n'a pas pu être extrait.", file=sys.stderr)
             sys.exit(0)
             
        # Conversion en objet datetime NAÏF
        date_parts = [int(p) for p in m.group(1).split(', ')]
        naive_expiry = datetime(*date_parts) # <--- NAÏF
        
        # ⚠️ Localisation et Conversion en UTC
        paris_tz = pytz.timezone('Europe/Paris')
        localized_expiry = paris_tz.localize(naive_expiry)
        expiry_time_utc = localized_expiry.astimezone(timezone.utc)
        
    except Exception as e:
        print(f"Erreur critique lors de l'extraction de la date d'expiration: {e}", file=sys.stderr)
        sys.exit(1)
        
    # Calcul du temps d'attente
    current_time_utc = datetime.now(timezone.utc)
    wait_time_seconds = int((expiry_time_utc.timestamp() + 120) - current_time_utc.timestamp())
    
    expiry_time_paris = expiry_time_utc.astimezone(paris_tz)
    print(f"Session expire le: {expiry_time_utc.isoformat()} (UTC) soit {expiry_time_paris.strftime('%Y-%m-%d %H:%M:%S')} (Paris). Temps restant: {wait_time_seconds} secondes.")
    # ----------------------------------------------------
    # 3. LOGIQUE DE DÉCISION
    # ----------------------------------------------------

    if wait_time_seconds <= 0:
        print("Avertissement: Session déjà expirée ou expiration imminente. Relance immédiate.")
        pass # On laisse le script descendre vers la partie 4
        
    elif wait_time_seconds <= MAX_WAIT_SECONDS:
        # Fenêtre courte : On dort
        print(f"Action: WAIT. Attente de {wait_time_seconds} secondes pour la fin de session.")
        time.sleep(wait_time_seconds)
        # Une fois réveillé, on laisse le script descendre vers la partie 4 👇
        
    else:
        # Fenêtre longue : On planifie et ON QUITTE
        
        # Vérif heure de fin Paris (20h00)
        paris_end_parking_timestamp = get_paris_end_of_parking_utc(current_time_utc).timestamp()      
        
        if expiry_time_utc.timestamp() > paris_end_parking_timestamp: # <--- CHANGEMENT DE VARIABLE
            print("Session se terminant après aujourd'hui 20h00 (heure de Paris), la relance sera gérée par le cron de demain matin.")
            sys.exit(0)
        
        # Calcul Dispatch
        dispatch_timestamp = expiry_time_utc.timestamp() - MARGIN_SECONDS
        dispatch_iso = datetime.fromtimestamp(dispatch_timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        
        print(f"Action: DISPATCH. Planification d'un job à {dispatch_iso}.")
        
        if not GH_TOKEN:
            print("Erreur: GH_PAT non trouvé.", file=sys.stderr)
            sys.exit(1)
            
        try:
            subprocess.run([
                "gh", "workflow", "run", "parking_payment_dispatch.yml", 
                "--ref", os.environ.get('GITHUB_REF_NAME', 'main'),
                "-f", f"launch_time={dispatch_iso}", 
                "-f", f"target_account={CONFIG_NAME}"
            ], check=True, stdout=sys.stdout, stderr=sys.stderr)
            print("Workflow de relance planifié avec succès.")
        except subprocess.CalledProcessError as e:
            print(f"Erreur lors de la planification: {e}", file=sys.stderr)
            sys.exit(1)

        sys.exit(0) # IMPORTANT : On quitte ici pour ne pas relancer le paiement tout de suite

    # ----------------------------------------------------
    # 4. SECONDE TENTATIVE (RELANCE APRÈS ATTENTE)
    # ----------------------------------------------------
    # Le code arrive ici seulement si on a fait "pass" ou fini le "sleep"
    
    print(">>> Relance du paiement maintenant...")
    
    # On réutilise exactement les mêmes arguments 'args' définis au début
    process_retry = subprocess.run(args, capture_output=True, text=True)
    
    print("--- Log de la relance ---")
    print(process_retry.stdout + process_retry.stderr)
    print("-------------------------")
    
    if process_retry.returncode == 0:
        print("Paiement de relance réussi !")
        sys.exit(0)
    else:
        print("Erreur lors du paiement de relance.", file=sys.stderr)
        sys.exit(process_retry.returncode)


def main():
    """Point d'entrée principal."""
    # 1. Injection des secrets dans le YAML
    inject_secrets()

    # 2. Exécution du paiement et analyse
    # Si le script doit attendre (sleep), il relancera le paiement lui-même
    # Sinon (dispatch, succès, échec), il terminera le job
    execute_payment_and_analyze()

if __name__ == "__main__":
    main()
