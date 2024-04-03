"""Handlers for all OLS-related REST API endpoints."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_core.messages.base import BaseMessage

from ols import constants
from ols.app import metrics
from ols.app.metrics import TokenMetricUpdater
from ols.app.models.models import LLMRequest, LLMResponse
from ols.src.llms.llm_loader import LLMConfigurationError, load_llm
from ols.src.query_helpers.chat_history import ChatHistory
from ols.src.query_helpers.docs_summarizer import DocsSummarizer
from ols.src.query_helpers.question_validator import QuestionValidator
from ols.utils import config, suid
from ols.utils.auth_dependency import AuthDependency
from ols.utils.keywords import KEYWORDS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
auth_dependency = AuthDependency(virtual_path="/ols-access")


@router.post("/query")
def conversation_request(
    llm_request: LLMRequest, auth: Any = Depends(auth_dependency)
) -> LLMResponse:
    """Handle conversation requests for the OLS endpoint.

    Args:
        llm_request: The request containing a query and conversation ID.
        auth: The Authentication handler (FastAPI Depends) that will handle authentication Logic.

    Returns:
        Response containing the processed information.
    """
    # Initialize variables
    previous_input = []
    referenced_documents: list[str] = []

    user_id = retrieve_user_id(auth)
    logger.info(f"User ID {user_id}")

    conversation_id = retrieve_conversation_id(llm_request)
    previous_input = retrieve_previous_input(user_id, llm_request)

    # Log incoming request
    logger.info(f"{conversation_id} Incoming request: {llm_request.query}")

    # Redact the query
    llm_request = redact_query(conversation_id, llm_request)

    # Validate the query
    if not previous_input:
        valid = validate_question(conversation_id, llm_request)
    else:
        logger.debug("follow-up conversation - skipping question validation")
        valid = True

    if not valid:
        response, referenced_documents, truncated = (
            constants.INVALID_QUERY_RESP,
            [],
            False,
        )
    else:
        response, referenced_documents, truncated = generate_response(
            conversation_id, llm_request, previous_input
        )

    store_conversation_history(user_id, conversation_id, llm_request, response)
    return LLMResponse(
        conversation_id=conversation_id,
        response=response,
        referenced_documents=referenced_documents,
        truncated=truncated,
    )


@router.post("/debug/query")
def conversation_request_debug_api(llm_request: LLMRequest) -> LLMResponse:
    """Handle requests for the base LLM completion endpoint.

    Args:
        llm_request: The request containing a query.

    Returns:
        Response containing the processed information.
    """
    conversation_id = retrieve_conversation_id(llm_request)
    logger.info(f"{conversation_id} Incoming request: {llm_request.query}")

    response = generate_bare_response(conversation_id, llm_request)

    return LLMResponse(
        conversation_id=conversation_id,
        response=response,
        referenced_documents=[],
        truncated=False,
    )


def retrieve_user_id(auth: Any) -> str:
    """Retrieve user ID from the token processed by auth. mechanism."""
    # auth contains tuple with user ID (in UUID format) and user name
    return auth[0]


def retrieve_conversation_id(llm_request: LLMRequest) -> str:
    """Retrieve conversation ID based on existing ID or on newly generated one."""
    conversation_id = llm_request.conversation_id

    # Generate a new conversation ID if not provided
    if not conversation_id:
        conversation_id = suid.get_suid()
        logger.info(f"{conversation_id} New conversation")

    return conversation_id


def retrieve_previous_input(user_id: str, llm_request: LLMRequest) -> list[BaseMessage]:
    """Retrieve previous user input, if exists."""
    previous_input: list[BaseMessage] = []
    if llm_request.conversation_id:
        cache_content = config.conversation_cache.get(
            user_id, llm_request.conversation_id
        )
        if cache_content is not None:
            previous_input = cache_content
        logger.info(
            f"{llm_request.conversation_id} Previous conversation input: {previous_input}"
        )
    return previous_input


def generate_response(
    conversation_id: str,
    llm_request: LLMRequest,
    previous_input: list[BaseMessage],
) -> tuple[str, list[str], bool]:
    """Generate response based on validation result, previous input, and model output."""
    # Summarize documentation
    try:
        docs_summarizer = DocsSummarizer(
            provider=llm_request.provider, model=llm_request.model
        )
        llm_response = docs_summarizer.summarize(
            conversation_id, llm_request.query, config.rag_index, previous_input
        )
        return (
            llm_response["response"],
            llm_response["referenced_documents"],
            llm_response["history_truncated"],
        )
    except Exception as summarizer_error:
        logger.error("Error while obtaining answer for user question")
        logger.exception(summarizer_error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error while obtaining answer for user question",
        )


def store_conversation_history(
    user_id: str, conversation_id: str, llm_request: LLMRequest, response: Optional[str]
) -> None:
    """Store conversation history into selected cache."""
    if config.conversation_cache is not None:
        logger.info(f"{conversation_id} Storing conversation history.")
        chat_message_history = ChatHistory.get_chat_message_history(
            llm_request.query, response or ""
        )
        config.conversation_cache.insert_or_append(
            user_id,
            conversation_id,
            chat_message_history,
        )


def redact_query(conversation_id: str, llm_request: LLMRequest) -> LLMRequest:
    """Redact query using query_redactor, raise HTTPException in case of any problem."""
    try:
        logger.debug(f"Redacting query for conversation {conversation_id}")
        if not config.query_redactor:
            logger.debug("query_redactor not found")
            return llm_request
        llm_request.query = config.query_redactor.redact_query(
            conversation_id, llm_request.query
        )
        return llm_request
    except Exception as redactor_error:
        logger.error(
            f"Error while redacting query {redactor_error} for conversation {conversation_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"response": f"Error while redacting query '{redactor_error}'"},
        )


def _validate_question_llm(conversation_id: str, llm_request: LLMRequest) -> bool:
    """Validate user question using llm, raise HTTPException in case of any problem."""
    # Validate the query
    try:
        question_validator = QuestionValidator(
            provider=llm_request.provider, model=llm_request.model
        )
        return question_validator.validate_question(conversation_id, llm_request.query)
    except LLMConfigurationError as e:
        metrics.llm_calls_validation_errors_total.inc()
        logger.error(e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"response": f"Unable to process this request because '{e}'"},
        )
    except Exception as validation_error:
        metrics.llm_calls_failures_total.inc()
        logger.error("Error while validating question")
        logger.exception(validation_error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error while validating question",
        )


def _validate_question_keyword(query: str) -> bool:
    """Validate user question using keyword."""
    # Current implementation is without any tokenizer method, lemmatization/n-grams.
    # Add valid keywords to keywords.py file.
    query_temp = query.lower()
    for kw in KEYWORDS:
        if kw in query_temp:
            return True
    # query_temp = {q_word.lower().strip(".?,") for q_word in query.split()}
    # common_words = config.keywords.intersection(query_temp)
    # if len(common_words) > 0:
    #     return constants.SUBJECT_ALLOWED

    logger.debug(f"No matching keyword found for query: {query}")
    return False


def validate_question(conversation_id: str, llm_request: LLMRequest) -> bool:
    """Validate user question."""
    match config.ols_config.query_validation_method:

        case constants.QueryValidationMethod.DISABLED:
            logger.debug(
                f"{conversation_id} Question validation is disabled. "
                f"Treating question as valid."
            )
            return True

        case constants.QueryValidationMethod.KEYWORD:
            logger.debug("Keyword based query validation.")
            return _validate_question_keyword(llm_request.query)

        case _:
            logger.debug("LLM based query validation.")
            return _validate_question_llm(conversation_id, llm_request)


def generate_bare_response(conversation_id: str, llm_request: LLMRequest) -> str:
    """Generate bare response without validation not using conversation history."""
    bare_llm = load_llm(
        config.ols_config.default_provider,
        config.ols_config.default_model,
    )

    prompt = PromptTemplate.from_template("{query}")
    llm_chain = LLMChain(llm=bare_llm, prompt=prompt, verbose=True)

    with TokenMetricUpdater(
        llm=bare_llm,
        provider=config.ols_config.default_provider,
        model=config.ols_config.default_model,
    ) as token_counter:
        response = llm_chain.invoke(
            input={"query": llm_request.query}, config={"callbacks": [token_counter]}
        )

    logger.info(f"{conversation_id} Model returned: {response}")
    return response["text"]
