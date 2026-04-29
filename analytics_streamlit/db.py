import os
import pyodbc
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    server = os.getenv("ANALYTICS_DB_SERVER")
    database = os.getenv("ANALYTICS_DB_DATABASE")
    username = os.getenv("ANALYTICS_DB_USERNAME")
    password = os.getenv("ANALYTICS_DB_PASSWORD")
    driver = os.getenv("ANALYTICS_DB_DRIVER", "{ODBC Driver 17 for SQL Server}")

    connection_string = f"""
        DRIVER={driver};
        SERVER={server};
        DATABASE={database};
        UID={username};
        PWD={password};
    """

    return pyodbc.connect(connection_string)