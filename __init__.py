"""Natural-language-to-SQL assistant with layered safety checks."""

from .assistant import Attempt, NL2SQLAssistant, QueryResult
from .executor import QueryExecutionError, QueryTimeout, SQLiteDatabase
from .llm import AnthropicClient, LLMError
from .validator import SQLValidationError, validate_sql

__version__ = "0.1.0"

__all__ = [
    "AnthropicClient",
    "Attempt",
    "LLMError",
    "NL2SQLAssistant",
    "QueryExecutionError",
    "QueryResult",
    "QueryTimeout",
    "SQLValidationError",
    "SQLiteDatabase",
    "validate_sql",
]
