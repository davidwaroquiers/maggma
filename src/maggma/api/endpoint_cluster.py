from typing import List, Dict, Union, Optional
from pydantic import BaseModel
from monty.json import MSONable
from fastapi import FastAPI, APIRouter, Path, HTTPException
from maggma.core import Store
from maggma.api import default_error_responses as default_responses
from maggma.utils import dynamic_import


class EndpointCluster(MSONable):
    """
    Implements an endpoint cluster which is a REST Compatible Resource as
    a URL endpoint
    """

    def __init__(
        self,
        store: Store,
        model: Union[BaseModel, str],
        tags: Optional[List[str]] = None,
        responses: Optional[Dict] = None,
    ):
        """
        Args:
            store: The Maggma Store to get data from
            model: the pydantic model to apply to the documents from the Store
                This can be a string with a full python path to a model or
                an actuall pydantic Model if this is being instantied in python
                code. Serializing this via Monty will autoconvert the pydantic model
                into a python path string
            tags: list of tags for the Endpoint
            responses: default responses for error codes
        """
        self.store = store
        self.router = APIRouter()
        self.tags = tags
        self.responses = responses

        if isinstance(model, BaseModel):
            self.model = model
        elif isinstance(model, str):
            module_path = ".".join(model.split(".")[:-1])
            class_name = model.split(".")[-1]
            dynamic_import(module_path, class_name)
        else:
            raise ValueError(
                "Model has to be a pydantic model or python path to a pydantic model"
            )

        self.prepare_endpoint()

    def prepare_endpoint(self):
        """
        Internal method to prepare the endpoint by setting up default handlers
        for routes
        """
        key_name = self.store.key
        model_name = self.model.__class__.__name__
        responses = dict(**default_responses)
        if self.responses:
            responses.update(self.responses)

        tags = self.tags or []

        async def get_by_key(
            self, key: str = Path(..., title=f"The {key_name} of the item to get")
        ):
            f"""
            Get's a document by the primary key in the store

            Args:
                {key_name}: the id of a single

            Returns:
                a single document that satisfies the {self.model.__class__.__name__} model
            """
            item = self.store.query_one(criteria={self.store.key: key})

            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Item with {self.store.key} = {key} not found",
                )
            else:
                model_item = self.model(**item)
                return model_item

        self.router.get(
            f"/{key_name}/{{task_id}}",
            response_description=f"Get an {model_name} by {key_name}",
            response_model=self.model,
            tags=tags,
            responses=responses,
        )(get_by_key)

    def run(self):
        """
        Runs the Endpoint cluster locally
        This is intended for testing not production
        """
        import uvicorn

        app = FastAPI()
        app.include_router(self.router, prefix="")
        uvicorn.run(app)

    def as_dict(self) -> Dict:
        """
        Special as_dict implemented to convert pydantic models into strings
        """

        d = super().as_dict()  # Ensures sub-classes serialize correctly
        d["model"] = f"{self.store.__class__.__module__}.{self.store.__class__.name}"

        for field in ["tags", "responses"]:
            if not d.get(field, None):
                del d[field]
        return d
