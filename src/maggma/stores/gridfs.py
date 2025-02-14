# coding: utf-8
"""
Module containing various definitions of Stores.
Stores are a default access pattern to data and provide
various utillities
"""

import copy
import json
import zlib
import yaml
from datetime import datetime
from pymongo.errors import ConfigurationError
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import gridfs
from monty.dev import deprecated
from monty.json import jsanitize
from pydash import get, has
from pymongo import MongoClient, uri_parser

from maggma.core import Sort, Store
from maggma.stores.mongolike import MongoStore

# https://github.com/mongodb/specifications/
#   blob/master/source/gridfs/gridfs-spec.rst#terms
#   (Under "Files collection document")
files_collection_fields = (
    "_id",
    "length",
    "chunkSize",
    "uploadDate",
    "md5",
    "filename",
    "contentType",
    "aliases",
    "metadata",
)


class GridFSStore(Store):
    """
    A Store for GrdiFS backend. Provides a common access method consistent with other stores
    """

    def __init__(
        self,
        database: str,
        collection_name: str,
        host: str = "localhost",
        port: int = 27017,
        username: str = "",
        password: str = "",
        compression: bool = False,
        ensure_metadata: bool = False,
        searchable_fields: List[str] = None,
        **kwargs,
    ):
        """
        Initializes a GrdiFS Store for binary data
        Args:
            database: database name
            collection_name: The name of the collection.
                This is the string portion before the GridFS extensions
            host: hostname for the database
            port: port to connect to
            username: username to connect as
            password: password to authenticate as
            compression: compress the data as it goes into GridFS
            ensure_metadata: ensure returned documents have the metadata fields
            searchable_fields: fields to keep in the index store
        """

        self.database = database
        self.collection_name = collection_name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._collection = None  # type: Any
        self.compression = compression
        self.ensure_metadata = ensure_metadata
        self.searchable_fields = [] if searchable_fields is None else searchable_fields
        self.kwargs = kwargs

        if "key" not in kwargs:
            kwargs["key"] = "_id"
        super().__init__(**kwargs)

    @classmethod
    def from_launchpad_file(cls, lp_file, collection_name, **kwargs):
        """
        Convenience method to construct a GridFSStore from a launchpad file

        Note: A launchpad file is a special formatted yaml file used in fireworks

        Returns:
        """
        with open(lp_file, "r") as f:
            lp_creds = yaml.load(f, Loader=yaml.FullLoader)

        db_creds = lp_creds.copy()
        db_creds["database"] = db_creds["name"]
        for key in list(db_creds.keys()):
            if key not in ["database", "host", "port", "username", "password"]:
                db_creds.pop(key)
        db_creds["collection_name"] = collection_name

        return cls(**db_creds, **kwargs)

    @property
    def name(self) -> str:
        """
        Return a string representing this data source
        """
        return f"gridfs://{self.host}/{self.database}/{self.collection_name}"

    def connect(self, force_reset: bool = False):
        """
        Connect to the source data
        """
        conn = MongoClient(self.host, self.port)
        if not self._collection or force_reset:
            db = conn[self.database]
            if self.username != "":
                db.authenticate(self.username, self.password)

            self._collection = gridfs.GridFS(db, self.collection_name)
            self._files_collection = db["{}.files".format(self.collection_name)]
            self._files_store = MongoStore.from_collection(self._files_collection)
            self._files_store.last_updated_field = f"metadata.{self.last_updated_field}"
            self._files_store.key = self.key
            self._chunks_collection = db["{}.chunks".format(self.collection_name)]

    @property  # type: ignore
    @deprecated(message="This will be removed in the future")
    def collection(self):
        return self._collection

    @property
    def last_updated(self) -> datetime:
        """
        Provides the most recent last_updated date time stamp from
        the documents in this Store
        """
        return self._files_store.last_updated

    @classmethod
    def transform_criteria(cls, criteria: Dict) -> Dict:
        """
        Allow client to not need to prepend 'metadata.' to query fields.
        Args:
            criteria: Query criteria
        """
        new_criteria = dict()
        for field in criteria:
            if field not in files_collection_fields and not field.startswith(
                "metadata."
            ):
                new_criteria["metadata." + field] = copy.copy(criteria[field])
            else:
                new_criteria[field] = copy.copy(criteria[field])

        return new_criteria

    def count(self, criteria: Optional[Dict] = None) -> int:
        """
        Counts the number of documents matching the query criteria

        Args:
            criteria: PyMongo filter for documents to count in
        """
        if isinstance(criteria, dict):
            criteria = self.transform_criteria(criteria)

        return self._files_store.count(criteria)

    def query(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Union[Sort, int]]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Dict]:
        """
        Queries the GridFS Store for a set of documents.
        Will check to see if data can be returned from
        files store first.
        If the data from the gridfs is not a json serialized string
        a dict will be returned with the data in the "data" key
        plus the self.key and self.last_updated_field.

        Args:
            criteria: PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields. Keys are field names and
                values are 1 for ascending or -1 for descending.
            skip: number documents to skip
            limit: limit on total number of documents returned
        """
        if isinstance(criteria, dict):
            criteria = self.transform_criteria(criteria)
        elif criteria is not None:
            raise ValueError("Criteria must be a dictionary or None")

        prop_keys = set()
        if isinstance(properties, dict):
            prop_keys = set(properties.keys())
        elif isinstance(properties, list):
            prop_keys = set(properties)

        for doc in self._files_store.query(
            criteria=criteria, sort=sort, limit=limit, skip=skip
        ):
            if properties is not None and prop_keys.issubset(set(doc.keys())):
                yield {p: doc[p] for p in properties if p in doc}
            else:

                metadata = doc.get("metadata", {})

                data = self._collection.find_one(
                    filter={"_id": doc["_id"]}, skip=skip, limit=limit, sort=sort,
                ).read()

                if metadata.get("compression", "") == "zlib":
                    data = zlib.decompress(data).decode("UTF-8")

                try:
                    data = json.loads(data)
                except Exception:
                    if not isinstance(data, dict):
                        data = {
                            "data": data,
                            self.key: doc.get(self.key),
                            self.last_updated_field: doc.get(self.last_updated_field),
                        }

                if self.ensure_metadata and isinstance(data, dict):
                    data.update(metadata)

                yield data

    def distinct(
        self, field: str, criteria: Optional[Dict] = None, all_exist: bool = False
    ) -> List:
        """
        Get all distinct values for a field. This function only operates
        on the metadata in the files collection

        Args:
            field: the field(s) to get distinct values for
            criteria: PyMongo filter for documents to search in
        """
        criteria = (
            self.transform_criteria(criteria)
            if isinstance(criteria, dict)
            else criteria
        )

        field = (
            f"metadata.{field}"
            if field not in files_collection_fields
            and not field.startswith("metadata.")
            else field
        )

        return self._files_store.distinct(field=field, criteria=criteria)

    def groupby(
        self,
        keys: Union[List[str], str],
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Union[Sort, int]]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Tuple[Dict, List[Dict]]]:
        """
        Simple grouping function that will group documents
        by keys. Will only work if the keys are included in the files
        collection for GridFS

        Args:
            keys: fields to group documents
            criteria: PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields. Keys are field names and
                values are 1 for ascending or -1 for descending.
            skip: number documents to skip
            limit: limit on total number of documents returned

        Returns:
            generator returning tuples of (dict, list of docs)
        """

        criteria = (
            self.transform_criteria(criteria)
            if isinstance(criteria, dict)
            else criteria
        )
        keys = [keys] if not isinstance(keys, list) else keys
        keys = [
            f"metadata.{k}"
            if k not in files_collection_fields and not k.startswith("metadata.")
            else k
            for k in keys
        ]
        for group, ids in self._files_store.groupby(
            keys, criteria=criteria, properties=[f"metadata.{self.key}"]
        ):
            ids = [
                get(doc, f"metadata.{self.key}")
                for doc in ids
                if has(doc, f"metadata.{self.key}")
            ]

            group = {
                k.replace("metadata.", ""): get(group, k) for k in keys if has(group, k)
            }

            yield group, list(self.query(criteria={self.key: {"$in": ids}}))

    def ensure_index(self, key: str, unique: Optional[bool] = False) -> bool:
        """
        Tries to create an index and return true if it suceeded
        Currently operators on the GridFS files collection
        Args:
            key: single key to index
            unique: Whether or not this index contains only unique keys

        Returns:
            bool indicating if the index exists/was created
        """
        # Transform key for gridfs first
        if key not in files_collection_fields:
            files_col_key = "metadata.{}".format(key)
            return self._files_store.ensure_index(files_col_key, unique=unique)
        else:
            return self._files_store.ensure_index(key, unique=unique)

    def update(
        self,
        docs: Union[List[Dict], Dict],
        key: Union[List, str, None] = None,
        additional_metadata: Union[str, List[str], None] = None,
    ):
        """
        Update documents into the Store

        Args:
            docs: the document or list of documents to update
            key: field name(s) to determine uniqueness for a
                 document, can be a list of multiple fields,
                 a single field, or None if the Store's key
                 field is to be used
            additional_metadata: field(s) to include in the gridfs metadata
        """

        if not isinstance(docs, list):
            docs = [docs]

        if isinstance(key, str):
            key = [key]
        elif not key:
            key = [self.key]

        key = list(set(key) - set(files_collection_fields))

        if additional_metadata is None:
            additional_metadata = []
        elif isinstance(additional_metadata, str):
            additional_metadata = [additional_metadata]
        else:
            additional_metadata = list(additional_metadata)

        for d in docs:
            search_doc = {k: d[k] for k in key}

            metadata = {
                k: get(d, k)
                for k in [self.last_updated_field]
                + additional_metadata
                + self.searchable_fields
                if has(d, k)
            }
            metadata.update(search_doc)
            data = json.dumps(jsanitize(d)).encode("UTF-8")
            if self.compression:
                data = zlib.compress(data)
                metadata["compression"] = "zlib"

            self._collection.put(data, metadata=metadata)
            search_doc = self.transform_criteria(search_doc)

            # Cleans up old gridfs entries
            for fdoc in (
                self._files_collection.find(search_doc, ["_id"])
                .sort("uploadDate", -1)
                .skip(1)
            ):
                self._collection.delete(fdoc["_id"])

    def remove_docs(self, criteria: Dict):
        """
        Remove docs matching the query dictionary

        Args:
            criteria: query dictionary to match
        """
        if isinstance(criteria, dict):
            criteria = self.transform_criteria(criteria)
        ids = [cursor._id for cursor in self._collection.find(criteria)]

        for _id in ids:
            self._collection.delete(_id)

    def close(self):
        self._collection.database.client.close()

    def __eq__(self, other: object) -> bool:
        """
        Check equality for GridFSStore
        other: other GridFSStore to compare with
        """
        if not isinstance(other, GridFSStore):
            return False

        fields = ["database", "collection_name", "host", "port"]
        return all(getattr(self, f) == getattr(other, f) for f in fields)


class GridFSURIStore(GridFSStore):
    """
    A Store for GridFS backend, with connection via a mongo URI string.

    This is expected to be a special mongodb+srv:// URIs that include client parameters
    via TXT records
    """

    def __init__(
        self,
        uri: str,
        collection_name: str,
        database: str = None,
        compression: bool = False,
        ensure_metadata: bool = False,
        searchable_fields: List[str] = None,
        **kwargs,
    ):
        """
        Initializes a GrdiFS Store for binary data
        Args:
            uri: MongoDB+SRV URI
            database: database to connect to
            collection_name: The collection name
            compression: compress the data as it goes into GridFS
            ensure_metadata: ensure returned documents have the metadata fields
            searchable_fields: fields to keep in the index store
        """

        self.uri = uri

        # parse the dbname from the uri
        if database is None:
            d_uri = uri_parser.parse_uri(uri)
            if d_uri["database"] is None:
                raise ConfigurationError(
                    "If database name is not supplied, a database must be set in the uri"
                )
            self.database = d_uri["database"]
        else:
            self.database = database

        self.collection_name = collection_name
        self._collection = None  # type: Any
        self.compression = compression
        self.ensure_metadata = ensure_metadata
        self.searchable_fields = [] if searchable_fields is None else searchable_fields
        self.kwargs = kwargs

        if "key" not in kwargs:
            kwargs["key"] = "_id"
        super(GridFSStore, self).__init__(**kwargs)  # lgtm

    def connect(self, force_reset: bool = False):
        """
        Connect to the source data
        """
        if not self._collection or force_reset:  # pragma: no cover
            conn = MongoClient(self.uri)
            db = conn[self.database]
            self._collection = gridfs.GridFS(db, self.collection_name)
            self._files_collection = db["{}.files".format(self.collection_name)]
            self._files_store = MongoStore.from_collection(self._files_collection)
            self._files_store.last_updated_field = f"metadata.{self.last_updated_field}"
            self._files_store.key = self.key
            self._chunks_collection = db["{}.chunks".format(self.collection_name)]
