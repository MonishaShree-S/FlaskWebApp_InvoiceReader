#config.py
import os
import configparser
import logging

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
    config = configparser.ConfigParser()
    try:
        config.read('app/config/db.properties')
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
