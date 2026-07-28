from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Customer:
    customer_id: str
    organization_id: str
    customer_no: str
    name: str
    email: str | None
    phone: str | None
    created_at: str


class CustomerNotFoundError(LookupError):
    pass


class CustomerAlreadyExistsError(ValueError):
    pass


class InvalidCustomerReferenceError(ValueError):
    pass


class CustomerStore(Protocol):
    def create_customer(
        self,
        *,
        organization_id: str,
        customer_no: str,
        name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> Customer:
        raise NotImplementedError

    def get_by_id(
        self,
        *,
        organization_id: str,
        customer_id: str,
    ) -> Customer:
        raise NotImplementedError

    def get_by_no(
        self,
        *,
        organization_id: str,
        customer_no: str,
    ) -> Customer:
        raise NotImplementedError
