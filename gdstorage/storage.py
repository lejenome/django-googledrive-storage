from __future__ import unicode_literals

import mimetypes
import os.path
from io import BytesIO
from functools import partial

import django
import enum
import httplib2
import six
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from dateutil.parser import parse
from django.conf import settings
from django.core.files import File
from django.core.files.storage import Storage
from google.oauth2 import service_account
from django.core.cache import caches


DJANGO_VERSION = django.VERSION[:2]


class GoogleDrivePermissionType(enum.Enum):
    """
        Describe a permission type for Google Drive as described on
        `Drive docs <https://developers.google.com/drive/v3/reference/permissions>`_
    """

    USER = "user"
    """
        Permission for single user
    """

    GROUP = "group"
    """
        Permission for group defined in Google Drive
    """

    DOMAIN = "domain"
    """
        Permission for domain defined in Google Drive
    """

    ANYONE = "anyone"
    """
        Permission for anyone
    """


class GoogleDrivePermissionRole(enum.Enum):
    """
        Describe a permission role for Google Drive as described on
        `Drive docs <https://developers.google.com/drive/v3/reference/permissions>`_
    """

    OWNER = "owner"
    """
        File Owner
    """

    READER = "reader"
    """
        User can read a file
    """

    WRITER = "writer"
    """
        User can write a file
    """

    COMMENTER = "commenter"
    """
        User can comment a file
    """


class GoogleDriveFilePermission(object):
    """
        Describe a permission for Google Drive as described on
        `Drive docs <https://developers.google.com/drive/v3/reference/permissions>`_

        :param gdstorage.GoogleDrivePermissionRole g_role: Role associated to this permission
        :param gdstorage.GoogleDrivePermissionType g_type: Type associated to this permission
        :param str g_email: email address that qualifies the User associated to this permission

    """

    @property
    def role(self):
        """
            Role associated to this permission

            :return: Enumeration that states the role associated to this permission
            :rtype: gdstorage.GoogleDrivePermissionRole
        """
        return self._role

    @property
    def type(self):
        """
            Type associated to this permission

            :return: Enumeration that states the role associated to this permission
            :rtype: gdstorage.GoogleDrivePermissionType
        """
        return self._type

    @property
    def email(self):
        """
            Email that qualifies the user associated to this permission
            :return: Email as string
            :rtype: str
        """
        return self._email

    @property
    def raw(self):
        """
            Transform the :class:`.GoogleDriveFilePermission` instance into a string used to issue the command to
            Google Drive API

            :return: Dictionary that states a permission compliant with Google Drive API
            :rtype: dict
        """

        result = {
            "role": self.role.value,
            "type": self.type.value
        }

        if self.email is not None:
            result["emailAddress"] = self.email

        return result

    def __init__(self, g_role, g_type, g_email=None):
        """
            Instantiate this class
        """
        if not isinstance(g_role, GoogleDrivePermissionRole):
            raise ValueError("Role should be a GoogleDrivePermissionRole instance")
        if not isinstance(g_type, GoogleDrivePermissionType):
            raise ValueError("Permission should be a GoogleDrivePermissionType instance")
        if g_email is not None and not isinstance(g_email, six.string_types):
            raise ValueError("emailAddress should be a String instance")

        self._role = g_role
        self._type = g_type
        self._email = g_email


_ANYONE_CAN_READ_PERMISSION_ = GoogleDriveFilePermission(
    GoogleDrivePermissionRole.READER,
    GoogleDrivePermissionType.ANYONE
)


class GoogleDriveStorage(Storage):
    """
    Storage class for Django that interacts with Google Drive as persistent storage.
    This class uses a system account for Google API that create an application drive
    (the drive is not owned by any Google User, but it is owned by the application declared on
    Google API console).
    """

    _UNKNOWN_MIMETYPE_ = "application/octet-stream"
    _GOOGLE_DRIVE_FOLDER_MIMETYPE_ = "application/vnd.google-apps.folder"

    def __init__(self, json_keyfile_path=None,
                 permissions=None, cache=None):
        """
        Handles credentials and builds the google service.

        :param json_keyfile_path: Path
        :param permissions: list
        :param cache: name of the cache backend to use for API requests. Set to None to disable caching
        :raise ValueError:
        """
        self._json_keyfile_path = json_keyfile_path or settings.GOOGLE_DRIVE_STORAGE_JSON_KEY_FILE

        credentials = service_account.Credentials.from_service_account_file(
            self._json_keyfile_path,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        # http = httplib2.Http(cache=".cache")
        # http = credentials.authorize(http)

        self._permissions = None
        if permissions is None:
            self._permissions = (_ANYONE_CAN_READ_PERMISSION_,)
        else:
            if not isinstance(permissions, (tuple, list,)):
                raise ValueError("Permissions should be a list or a tuple of GoogleDriveFilePermission instances")
            else:
                for p in permissions:
                    if not isinstance(p, GoogleDriveFilePermission):
                        raise ValueError(
                            "Permissions should be a list or a tuple of GoogleDriveFilePermission instances")
                # Ok, permissions are good
                self._permissions = permissions

        self._drive_service = build('drive', 'v3',
                                    # http=http,
                                    credentials=credentials)

        if cache:
            self._cache = caches[cache]
        else:
            self._cache = False

        # cache of folders Id. format {(title, parent_id): folder_id}
        self._foldersId = {}

    def _split_path(self, p):
        """
        Split a complete path in a list of strings

        :param p: Path to be splitted
        :type p: string
        :returns: list - List of strings that composes the path
        """
        p = p[1:] if p[0] == '/' else p
        a, b = os.path.split(p)
        return (self._split_path(a) if len(a) and len(b) else []) + [b]

    def _get_or_create_folder(self, path, parent_id=None):
        """
        Create a folder on Google Drive.
        It creates folders recursively.
        If the folder already exists, it retrieves only the unique identifier.

        :param path: Path that had to be created
        :type path: string
        :param parent_id: Unique identifier for its parent (folder)
        :type parent_id: string
        :returns: dict
        """
        folder_data = self._check_file_exists(path, parent_id)
        if folder_data is None:
            # Folder does not exists, have to create
            split_path = self._split_path(path)

            if split_path[:-1]:
                parent_path = os.path.join(*split_path[:-1])
                current_folder_data = self._get_or_create_folder(parent_path, parent_id=parent_id)
            else:
                current_folder_data = None

            meta_data = {
                'name': split_path[-1],
                'mimeType': self._GOOGLE_DRIVE_FOLDER_MIMETYPE_
            }
            if current_folder_data is not None:
                meta_data['parents'] = [{'id': current_folder_data['id']}]
            else:
                # This is the first iteration loop so we have to set the parent_id
                # obtained by the user, if available
                if parent_id is not None:
                    meta_data['parents'] = [{'id': parent_id}]
            current_folder_data = self._drive_service.files().create(body=meta_data).execute()
            return current_folder_data
        else:
            return folder_data

    def _get_file(self, filename):
        """
        Get the file with specific parameters from Google Drive.

        :param filename: File or folder to search
        :type filename: string
        :param use_cache: Use Django cache backend to store response for next requests
        :type use_cache: bool
        :returns: dict containing file / folder data if exists or None if does not exists
        """
        if not self._cache:
            return self._check_file_exists(filename)
        else:
            return self._cache.get_or_set(
                'gdstorage::' + filename,
                partial(self._check_file_exists, filename),
                timeout=24 * 60 * 60
            )

    def _check_file_exists(self, filename, parent_id=None):
        """
        Check if a file with specific parameters exists in Google Drive.

        :param filename: File or folder to search
        :type filename: string
        :param parent_id: Unique identifier for its parent (folder)
        :type parent_id: string
        :param mime_type: Mime Type for the file to search
        :type mime_type: string
        :returns: dict containing file / folder data if exists or None if does not exists
        """
        split_filename = self._split_path(filename)
        if len(split_filename) > 1:
            folder_id = self._foldersId.get((filename, parent_id), None)

            if folder_id is None:
                # This is an absolute path with folder inside
                # First check if the first element exists as a folder
                # If so call the method recursively with next portion of path
                # Otherwise the path does not exists hence the file does not exists
                q = "mimeType = '{0}' and name = '{1}'".format(self._GOOGLE_DRIVE_FOLDER_MIMETYPE_,
                                                                split_filename[0])
                if parent_id is not None:
                    q = "{0} and '{1}' in parents".format(q, parent_id)
                max_results = 1000  # Max value admitted from google drive
                folders = self._drive_service.files().list(q=q, pageSize=max_results, fields="files(name,id)").execute()
                for folder in folders["files"]:
                    if folder["name"] == split_filename[0]:
                        # Assuming every folder has a single parent
                        folder_id = folder["id"]
                        self._foldersId[(filename, parent_id)] = folder_id
                        break
            if folder_id is not None:
                return self._check_file_exists(os.path.sep.join(split_filename[1:]), folder_id)
            else:
                return None
        else:
            # This is a file, checking if exists
            q = "name = '{0}'".format(split_filename[0])
            if parent_id is not None:
                q = "{0} and '{1}' in parents".format(q, parent_id)
            max_results = 1000  # Max value admitted from google drive
            file_list = self._drive_service.files().list(q=q, pageSize=max_results, fields="files(id,name,originalFilename,webContentLink,size,createdTime,modifiedTime)").execute()
            if len(file_list["files"]) == 0:
                q = "" if parent_id is None else "'{0}' in parents".format(parent_id)
                file_list = self._drive_service.files().list(q=q, pageSize=max_results, fields="files(id,name,originalFilename,webContentLink,size,createdTime,modifiedTime)").execute()
                for element in file_list["files"]:
                    if split_filename[0] in element["name"]:
                        return element
                return None
            else:
                return file_list["files"][0]

    # Methods that had to be implemented
    # to create a valid storage for Django

    def _open(self, name, mode='rb'):
        file_data = self._get_file(name)
        response, content = self._drive_service._http.request(
            # file_data['downloadUrl'])
            file_data['webContentLink'])

        return File(BytesIO(content), name)

    def _save(self, name, content):
        folder_path = os.path.sep.join(self._split_path(name)[:-1])
        folder_data = self._get_or_create_folder(folder_path)
        parent_id = None if folder_data is None else folder_data['id']
        # Now we had created (or obtained) folder on GDrive
        # Upload the file
        mime_type, encoding = mimetypes.guess_type(name)
        if mime_type is None:
            mime_type = self._UNKNOWN_MIMETYPE_
        media_body = MediaIoBaseUpload(content.file, mime_type, resumable=True, chunksize=1024*1024*5)
        filename = os.path.basename(name)
        body = {
            'name': filename,
            'mimeType': mime_type
        }
        # Set the parent folder.
        if parent_id:
            body['parents'] = [parent_id]
        file_data = self._drive_service.files().create(
            body=body,
            media_body=media_body).execute()

        # Setting up permissions
        for p in self._permissions:
            self._drive_service.permissions().create(fileId=file_data["id"], body=p.raw).execute()

        return file_data.get(u'originalFilename', file_data.get('name'))

    def delete(self, name):
        """
        Deletes the specified file from the storage system.
        """
        file_data = self._get_file(name)
        if file_data is not None:
            self._drive_service.files().delete(fileId=file_data['id']).execute()

    def exists(self, name):
        """
        Returns True if a file referenced by the given name already exists in the
        storage system, or False if the name is available for a new file.
        """
        return self._get_file(name) is not None

    def listdir(self, path):
        """
        Lists the contents of the specified path, returning a 2-tuple of lists;
        the first item being directories, the second item being files.
        """
        directories, files = [], []
        if path == "/":
            folder_id = {"id": "root"}
        else:
            folder_id = self._get_file(path)
        if folder_id:
            file_params = {
                'q': "'{0}' in parents and mimeType != '{1}'".format(folder_id["id"],
                                                                     self._GOOGLE_DRIVE_FOLDER_MIMETYPE_),
                'fields': "files(name)",
            }
            dir_params = {
                'q': "'{0}' in parents and mimeType = '{1}'".format(folder_id["id"],
                                                                    self._GOOGLE_DRIVE_FOLDER_MIMETYPE_),
                'fields': "files(name)",
            }
            files_list = self._drive_service.files().list(**file_params).execute()
            dir_list = self._drive_service.files().list(**dir_params).execute()
            for element in files_list["files"]:
                files.append(os.path.join(path, element["name"]))
            for element in dir_list["files"]:
                directories.append(os.path.join(path, element["name"]))
        return directories, files

    def size(self, name):
        """
        Returns the total size, in bytes, of the file specified by name.
        """
        file_data = self._get_file(name)
        if file_data is None:
            return 0
        else:
            return file_data["size"]

    def url(self, name):
        """
        Returns an absolute URL where the file's contents can be accessed
        directly by a Web browser.
        """
        file_data = self._get_file(name)
        if file_data is None:
            return None
        else:
            return file_data["webContentLink"]

    def accessed_time(self, name):
        """
        Returns the last accessed time (as datetime object) of the file
        specified by name.
        """
        return self.modified_time(name)

    def created_time(self, name):
        """
        Returns the creation time (as datetime object) of the file
        specified by name.
        """
        file_data = self._get_file(name)
        if file_data is None:
            return None
        else:
            return parse(file_data['createdTime'])

    def modified_time(self, name):
        """
        Returns the last modified time (as datetime object) of the file
        specified by name.
        """
        file_data = self._get_file(name)
        if file_data is None:
            return None
        else:
            return parse(file_data["modifiedTime"])


if DJANGO_VERSION >= (1, 7):
    from django.utils.deconstruct import deconstructible


    @deconstructible
    class GoogleDriveStorage(GoogleDriveStorage):
        def deconstruct(self):
            """
                Handle field serialization to support migration

            """
            name, path, args, kwargs = \
                super(GoogleDriveStorage, self).deconstruct()
            if self._service_email is not None:
                kwargs["service_email"] = self._service_email
            if self._json_keyfile_path is not None:
                kwargs["json_keyfile_path"] = self._json_keyfile_path

    @deconstructible
    class GoogleDriveFilePermission(GoogleDriveFilePermission):
        def deconstruct(self):
            """
            Add a deconstructor to make the object serializable inorder to support migration
            
            """
            name, path, args, kwargs = \
                super(GoogleDriveFilePermission,self).deconstruct()
