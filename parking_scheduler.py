import os
import sys
import yaml
import json
from datetime import datetime, timedelta, timezone
import time
import subprocess
import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration des Limites ---
MAX_WAIT_SECONDS = 14400  # 4 heures
MARGIN_SECONDS = 2700     # 45 minutes pour la relance
SAFETY_GAP_SECONDS = 180  # 3 minutes de délai après expiration pour relance
PARIS_END_PARKING_HOUR_LOCAL = 20 # Heure de fin de stationnement à Paris (20:00 heure locale)

CONFIG_PATH = os.environ.get('CONFIG_FILE', './paybybot3.yml')
CONFIG_NAME = os.environ.get('CONFIG_ACCOUNT_NAME')
GH_TOKEN = os.environ.get('GH_PAT')
REPO_SLUG = os.environ.get('GITHUB_REPOSITORY')

# --- Constantes Google Sheets ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID') 
WORKSHEET_NAME = os.environ.get('GOOGLE_WORKSHEET_NAME', 'Absences') 
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT')
# --------------------------------


def get_paris_end_of_parking_utc(today_utc: datetime) -> datetime:
    """Calcule le timestamp de fin de stationnement (20h00 Paris) pour la date du jour, en UTC."""
    
    paris_tz = pytz.timezone('Europe/Paris')
    
    # 1. Obtenir la date du jour à partir de l'heure UTC du runner
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
    
    # Injection des valeurs
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


def get_next_absence_date(current_date_utc: datetime) -> datetime | None:
    """
    Se connecte à Google Sheets, lit les dates d'absence et retourne la première date UTC 
    de la prochaine absence (à minuit Paris), sinon None.
    """
    if not SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        print("Avertissement: Variables Google Sheets non configurées. La durée de paiement ne sera pas ajustée.")
        return None

    try:
        # 1. Authentification
        credentials_data = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_data, scope)
        client = gspread.authorize(creds)

        # 2. Lecture des données
        sheet = client.open_by_key(SHEET_ID)
        worksheet = sheet.worksheet(WORKSHEET_NAME)
        # On lit toutes les dates de la première colonne (col_values(1))
        dates_absences = worksheet.col_values(1)

        # 3. Logique de tri
        paris_tz = pytz.timezone('Europe/Paris')
        future_absences = []
        # Utiliser l'heure de début du job (pour déterminer "aujourd'hui")
        today_paris_midnight = current_date_utc.astimezone(paris_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        
        for date_str in dates_absences:
            if not date_str or 'date' in date_str.lower(): # Ignorer les lignes vides ou l'en-tête "Date"
                continue
                
            try:
                # La date doit être au format AAAA-MM-JJ
                absence_date_naive = datetime.strptime(date_str.strip(), '%Y-%m-%d')
                # Date d'absence à minuit (début du jour) en heure de Paris
                absence_date_paris = paris_tz.localize(absence_date_naive.replace(hour=0, minute=0, second=0, microsecond=0))
                
                # On ne considère que les absences futures ou qui commencent aujourd'hui
                if absence_date_paris >= today_paris_midnight:
                    future_absences.append(absence_date_paris.astimezone(timezone.utc))
            except ValueError:
                print(f"Avertissement: Format de date (AAAA-MM-JJ) invalide dans Google Sheets: {date_str.strip()}")
                continue
                
        if not future_absences:
            return None
            
        # Retourner la première (la plus proche) date d'absence en UTC
        return min(future_absences)

    except gspread.exceptions.WorksheetNotFound:
        print(f"Erreur: L'onglet '{WORKSHEET_NAME}' est introuvable. Paiement maximal tenté.", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Erreur critique lors de l'accès à Google Sheets: {e}. Paiement maximal tenté.", file=sys.stderr)
        return None


def execute_payment_and_analyze():
    """Exécute paybybot3, analyse la sortie et prend une décision."""
    
    MAX_DURATION_DAYS = int(os.environ['PBP_DURATION'])
    payment_duration_days = MAX_DURATION_DAYS
    current_time_utc = datetime.now(timezone.utc)
    paris_tz = pytz.timezone('Europe/Paris')

    # ----------------------------------------------------
    # 0. VÉRIFICATION DES ABSENCES ET AJUSTEMENT DE LA DURÉE
    # ----------------------------------------------------
    next_absence_utc = get_next_absence_date(current_time_utc)

    if next_absence_utc:
        # Calculer le temps écoulé entre maintenant et l'absence
        time_until_absence = next_absence_utc.timestamp() - current_time_utc.timestamp()
        
        # Calculer le nombre de jours entiers *maximum* que l'on peut payer
        # Une seconde de moins de 24h = 0 jour entier pour être sûr de ne pas empiéter.
        days_until_absence_end = int(time_until_absence / (24 * 3600))
        
        # Si nous sommes le jour de l'absence (ou après) on ne paye pas
        if days_until_absence_end <= 0:
            print(f"🚫 ABSENCE DÉTECTÉE AUJOURD'HUI: L'absence commence ce jour ({next_absence_utc.astimezone(paris_tz).strftime('%Y-%m-%d')}). Fin du job.")
            sys.exit(0)

        # Ajuster la durée: Payer au plus la durée max (6j) ET au plus la durée avant l'absence
        # On utilise days_until_absence_end pour être sûr que le paiement se termine
        # la veille ou le jour même avant l'heure de début. On laisse PayByPhone arrondir.
        payment_duration_days = min(MAX_DURATION_DAYS, days_until_absence_end)
        
        print(f"🏖️ ABSENCE DÉTECTÉE : Prochaine absence à partir de {next_absence_utc.astimezone(paris_tz).strftime('%Y-%m-%d')}.")
        print(f"Durée de paiement ajustée à : {payment_duration_days} jours (max. {MAX_DURATION_DAYS} jours).")

    # Si la durée est de 0 jour après ajustement (ex: l'absence commence demain et on lance à 6h), on s'arrête.
    if payment_duration_days <= 0:
        print("Durée de paiement ajustée à 0 jour ou moins. Fin du job.")
        sys.exit(0)
        
    # Les arguments paybybot3 (mis à jour)
    args = [
        sys.executable, "-m", "paybybot3", "pay", CONFIG_NAME,
        "--config", CONFIG_PATH,
        "--location", os.environ['PBP_LOCATION'],
        "--rate", os.environ['PBP_RATE'],
        "--duration", str(payment_duration_days),
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
             
        # Conversion en objet datetime et DÉCLARATION EN UTC (car l'API PayByPhone le renvoie en UTC)
        date_parts = [int(p) for p in m.group(1).split(', ')]
        expiry_time_utc = datetime(*date_parts, tzinfo=timezone.utc)
        
    except Exception as e:
        print(f"Erreur critique lors de l'extraction de la date d'expiration: {e}", file=sys.stderr)
        sys.exit(1)
        
    # Calcul du temps d'attente (jusqu'à l'expiration + marge de sécurité)
    wait_time_seconds = int((expiry_time_utc.timestamp() + SAFETY_GAP_SECONDS) - current_time_utc.timestamp())

    # --- AFFICHAGE CLAIR (UTC et Paris Local) ---
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
        
        if expiry_time_utc.timestamp() > paris_end_parking_timestamp:
            print("Session se terminant après aujourd'hui 20h00 (heure de Paris), la relance sera gérée par le cron de demain matin.")
            sys.exit(0)
        
        # Calcul Dispatch
        dispatch_timestamp = expiry_time_utc.timestamp() + SAFETY_GAP_SECONDS - MARGIN_SECONDS
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
    # Le script va déterminer la durée à payer, exécuter le paiement,
    # puis gérer la relance (sleep ou dispatch)
    execute_payment_and_analyze()

if __name__ == "__main__":
    main()
