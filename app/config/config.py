#config.py
import os
import configparser
import logging
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

logger = logging.getLogger(__name__)
class Config:
    """
    Base configuration class for the Flask app.
    """
    SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(24))
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///kaufland.db')  # Update with your database URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    #UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'uploads')
    #IMAGE_FOLDER = os.path.join(os.getcwd(), 'static', 'images')
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
    IMAGE_FOLDER = os.path.join(BASE_DIR, 'static', 'images')

    # Allowed file types for upload
    ALLOWED_EXTENSIONS = {'pdf'}

    # Auto-import pipeline: incoming/<Shop>/*.pdf is watched, parsed, and
    # deduped through the same process_one_pdf_upload() used by the manual
    # upload routes, then filed into processed/duplicates/needs_review/failed
    # under the same root -- see app/services/incoming_watcher.py.
    INCOMING_ROOT = os.environ.get('INCOMING_ROOT', r'C:\SiranthaRechnungSplit')
    INCOMING_FOLDER = os.path.join(INCOMING_ROOT, 'incoming')
    PROCESSED_FOLDER = os.path.join(INCOMING_ROOT, 'processed')
    DUPLICATES_FOLDER = os.path.join(INCOMING_ROOT, 'duplicates')
    NEEDS_REVIEW_FOLDER = os.path.join(INCOMING_ROOT, 'needs_review')
    FAILED_FOLDER = os.path.join(INCOMING_ROOT, 'failed')
    AUTO_IMPORT_USERID = int(os.environ['AUTO_IMPORT_USERID']) if os.environ.get('AUTO_IMPORT_USERID') else None
    config = configparser.ConfigParser()
    try:
        config.read(os.path.join(BASE_DIR, 'config', 'db.properties'))
        username = config.get('database_config', 'username')
        password = config.get('database_config', 'password')
        server = config.get('database_config', 'server')
        driver = config.get('database_config', 'driver')
        database = config.get('database_config', 'database')
    except configparser.Error as e:
        print(f"Error reading configuration file: {e}")

    connection_string = f'mssql+pyodbc://{username}:{password}@{server}/{database}?driver={driver.replace(" ", "+")}'
    logger.info(f"Connection string: {connection_string}")
    SQLALCHEMY_DATABASE_URI = connection_string
    SESSION_TYPE = 'filesystem'  # Store session data on the file system
    SESSION_PERMANENT = False
    #SESSION_FILE_DIR = './flask_sessions'
    SESSION_CACHELIB = './flask_sessions'

    #SESSION_FILE_DIR = os.path.join(os.getcwd(), 'flask_session')

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False
