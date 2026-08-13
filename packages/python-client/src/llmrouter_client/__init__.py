"""Official LLM Router Python client scaffold."""

from llmrouter_client.client import Client
from llmrouter_client.contracts import ContractValidationError, validate_contract

__all__ = ["Client", "ContractValidationError", "validate_contract"]
