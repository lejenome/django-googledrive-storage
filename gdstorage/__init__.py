from appconf import AppConf

__version__ = '1.0.0'


class GoogleDriveStorageConf(AppConf):

    class Meta:
        prefix = 'GOOGLE_DRIVE_STORAGE'

    USER_EMAIL = None
    JSON_KEY_FILE = None
    SERVICE_ACCOUNT_INFO = None
