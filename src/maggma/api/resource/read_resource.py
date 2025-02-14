from inspect import signature
from typing import Any, Dict, List, Optional, Type

from fastapi import Depends, HTTPException, Path, Request
from fastapi import Response as BareResponse
from pydantic import BaseModel

from maggma.api.models import Meta, Response
from maggma.api.query_operator import PaginationQuery, QueryOperator, SparseFieldsQuery
from maggma.api.resource import Resource
from maggma.api.resource.utils import attach_query_ops
from maggma.api.utils import STORE_PARAMS, merge_queries, object_id_serilaization_helper
from maggma.core import Store

import orjson


class ReadOnlyResource(Resource):
    """
    Implements a REST Compatible Resource as a GET URL endpoint
    This class provides a number of convenience features
    including full pagination, field projection
    """

    def __init__(
        self,
        store: Store,
        model: Type[BaseModel],
        tags: Optional[List[str]] = None,
        query_operators: Optional[List[QueryOperator]] = None,
        key_fields: Optional[List[str]] = None,
        query: Optional[Dict] = None,
        enable_get_by_key: bool = True,
        enable_default_search: bool = True,
        disable_validation: bool = False,
        include_in_schema: Optional[bool] = True,
        sub_path: Optional[str] = "/",
    ):
        """
        Args:
            store: The Maggma Store to get data from
            model: The pydantic model this Resource represents
            tags: List of tags for the Endpoint
            query_operators: Operators for the query language
            key_fields: List of fields to always project. Default uses SparseFieldsQuery
                to allow user to define these on-the-fly.
            enable_get_by_key: Enable default key route for endpoint.
            enable_default_search: Enable default endpoint search behavior.
            disable_validation: Whether to use ORJSON and provide a direct FastAPI response.
                Note this will disable auto JSON serialization and response validation with the
                provided model.
            include_in_schema: Whether the endpoint should be shown in the documented schema.
            sub_path: sub-URL path for the resource.
        """
        self.store = store
        self.tags = tags or []
        self.query = query or {}
        self.key_fields = key_fields
        self.versioned = False
        self.enable_get_by_key = enable_get_by_key
        self.enable_default_search = enable_default_search
        self.disable_validation = disable_validation
        self.include_in_schema = include_in_schema
        self.sub_path = sub_path
        self.response_model = Response[model]  # type: ignore

        self.query_operators = (
            query_operators
            if query_operators is not None
            else [
                PaginationQuery(),
                SparseFieldsQuery(
                    model,
                    default_fields=[self.store.key, self.store.last_updated_field],
                ),
            ]
        )

        super().__init__(model)

    def prepare_endpoint(self):
        """
        Internal method to prepare the endpoint by setting up default handlers
        for routes
        """

        if self.enable_get_by_key:
            self.build_get_by_key()

        if self.enable_default_search:
            self.build_dynamic_model_search()

    def build_get_by_key(self):
        key_name = self.store.key
        model_name = self.model.__name__

        if self.key_fields is None:
            field_input = SparseFieldsQuery(
                self.model, [self.store.key, self.store.last_updated_field]
            ).query
        else:

            def field_input():
                return {"properties": self.key_fields}

        async def get_by_key(
            key: str = Path(
                ..., alias=key_name, title=f"The {key_name} of the {model_name} to get",
            ),
            fields: STORE_PARAMS = Depends(field_input),
        ):
            f"""
            Get's a document by the primary key in the store

            Args:
                {key_name}: the id of a single {model_name}

            Returns:
                a single {model_name} document
            """
            self.store.connect()

            item = [
                self.store.query_one(
                    criteria={self.store.key: key, **self.query},
                    properties=fields["properties"],
                )
            ]

            if item == [None]:
                raise HTTPException(
                    status_code=404,
                    detail=f"Item with {self.store.key} = {key} not found",
                )

            for operator in self.query_operators:
                item = operator.post_process(item)

            response = {"data": item}

            if self.disable_validation:
                response = BareResponse(  # type: ignore
                    orjson.dumps(response, default=object_id_serilaization_helper)
                )

            return response

        self.router.get(
            f"{self.sub_path}{{{key_name}}}/",
            summary=f"Get a {model_name} document by by {key_name}",
            response_description=f"Get a {model_name} document by {key_name}",
            response_model=self.response_model,
            response_model_exclude_unset=True,
            tags=self.tags,
            include_in_schema=self.include_in_schema,
        )(get_by_key)

    def build_dynamic_model_search(self):

        model_name = self.model.__name__

        async def search(**queries: Dict[str, STORE_PARAMS]) -> Dict:
            request: Request = queries.pop("request")  # type: ignore

            query_params = [
                entry
                for _, i in enumerate(self.query_operators)
                for entry in signature(i.query).parameters
            ]

            overlap = [
                key for key in request.query_params.keys() if key not in query_params
            ]
            if any(overlap):
                raise HTTPException(
                    status_code=400,
                    detail="Request contains query parameters which cannot be used: {}".format(
                        ", ".join(overlap)
                    ),
                )

            query: Dict[Any, Any] = merge_queries(list(queries.values()))  # type: ignore
            query["criteria"].update(self.query)

            self.store.connect()

            count = self.store.count(query["criteria"])
            data = list(self.store.query(**query))
            operator_meta = {}

            for operator in self.query_operators:
                data = operator.post_process(data)
                operator_meta.update(operator.meta())

            meta = Meta(total_doc=count)

            response = {"data": data, "meta": {**meta.dict(), **operator_meta}}

            if self.disable_validation:
                response = BareResponse(  # type: ignore
                    orjson.dumps(response, default=object_id_serilaization_helper)
                )

            return response

        self.router.get(
            self.sub_path,
            tags=self.tags,
            summary=f"Get {model_name} documents",
            response_model=self.response_model,
            response_description=f"Search for a {model_name}",
            response_model_exclude_unset=True,
        )(attach_query_ops(search, self.query_operators))
