import os
import traceback
from fastapi import APIRouter, WebSocket
import openai
from ..config import Config
from ..llm import stream_gemini_response, stream_openai_response
from openai.types.chat import ChatCompletionMessageParam
from ..mock_llm import mock_completion
from typing import Dict, List
from ..image_generation import create_alt_url_mapping, generate_images
from ..prompts import assemble_imported_code_prompt, assemble_prompt
from ..access_token import validate_access_token
from datetime import datetime
import json

from ..utils import pprint_prompt  # type: ignore


router = APIRouter()


def write_logs(prompt_messages: List[ChatCompletionMessageParam], completion: str):
    # Get the logs path from environment, default to the current working directory
    logs_path = os.environ.get("LOGS_PATH", os.getcwd())

    # Create run_logs directory if it doesn't exist within the specified logs path
    logs_directory = os.path.join(logs_path, "run_logs")
    if not os.path.exists(logs_directory):
        os.makedirs(logs_directory)

    print("Writing to logs directory:", logs_directory)

    # Generate a unique filename using the current timestamp within the logs directory
    filename = datetime.now().strftime(f"{logs_directory}/messages_%Y%m%d_%H%M%S.json")

    # Write the messages dict into a new file for each run
    with open(filename, "w") as f:
        f.write(json.dumps({"prompt": prompt_messages, "completion": completion}))


@router.websocket("/generate-code")
async def stream_code(websocket: WebSocket):
    await websocket.accept()

    print("Incoming websocket connection...")

    async def throw_error(
        message: str,
    ):
        await websocket.send_json({"type": "error", "value": message})
        await websocket.close()

    # TODO: Are the values always strings?
    params: Dict[str, str] = await websocket.receive_json()

    print("Received params")

    # Read the code config settings from the request. Fall back to default if not provided.
    generated_code_config = ""
    if "generatedCodeConfig" in params and params["generatedCodeConfig"]:
        generated_code_config = params["generatedCodeConfig"]
    print(f"Generating {generated_code_config} code")

    # Get the OpenAI API key from the request. Fall back to environment variable if not provided.
    # If neither is provided, we throw an error.
    openai_api_key = Config.API_KEY if not Config.IS_MODEL_GEMINI else None
    openai_base_url = None
    google_api_key = Config.API_KEY if Config.IS_MODEL_GEMINI else None
    
    should_generate_images = False
    
    if "accessCode" in params and params["accessCode"]:
        print("Access code - using platform API key")
        res = await validate_access_token(params["accessCode"])
        if res["success"]:
            openai_api_key = os.environ.get("PLATFORM_OPENAI_API_KEY")
            google_api_key = os.environ.get("PLATFORM_GOOGLE_API_KEY")
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "value": res["failure_reason"],
                }
            )
            return
    else:
        if params['model'] == 'models/gemini-pro-vision':
            print('Using the Gemini Pro Vision Model')
            
            Config.MODEL = 'gemini'
            Config.IS_MODEL_GEMINI = True
            google_api_key = params['googleApiKey']
            if google_api_key:
                Config.API_KEY = google_api_key
                print("Using Google API key from client-side settings dialog")
        else:
            print('Using the GPT 4 Vision Model')
            if params["openAiApiKey"]:
                openai_api_key = params["openAiApiKey"]
                if openai_api_key:
                    Config.API_KEY = openai_api_key
                    print("Using OpenAI API key from client-side settings dialog")
            else:
                if openai_api_key:
                    print("Using OpenAI API key from environment variable")

    if (not Config.IS_MODEL_GEMINI and not Config.API_KEY) or (Config.IS_MODEL_GEMINI and not Config.API_KEY):
        print("API key not found")
        await websocket.send_json(
            {
                "type": "error",
                "value": "No API key found. Please add your API key in the settings dialog or add it to backend/.env file.",
            }
        )
        return

    if not Config.IS_MODEL_GEMINI:
        # Get the OpenAI Base URL from the request. Fall back to environment variable if not provided.
        openai_base_url = None
        # Disable user-specified OpenAI Base URL in prod
        if not os.environ.get("Config.IS_PROD"):
            if "openAiBaseURL" in params and params["openAiBaseURL"]:
                openai_base_url = params["openAiBaseURL"]
                print("Using OpenAI Base URL from client-side settings dialog")
            else:
                openai_base_url = os.environ.get("OPENAI_BASE_URL")
                if openai_base_url:
                    print("Using OpenAI Base URL from environment variable")

        if not openai_base_url:
            print("Using official OpenAI URL")

        # Get the image generation flag from the request. Fall back to True if not provided.
        should_generate_images = (
            params["isImageGenerationEnabled"]
            if "isImageGenerationEnabled" in params
            else True
        )

        print("generating code...")
        await websocket.send_json({"type": "status", "value": "Generating code..."})

    async def process_chunk(content: str):
        await websocket.send_json({"type": "chunk", "value": content})

    # Image cache for updates so that we don't have to regenerate images
    image_cache: Dict[str, str] = {}

    # If this generation started off with imported code, we need to assemble the prompt differently
    if params.get("isImportedFromCode") and params["isImportedFromCode"]:
        original_imported_code = params["history"][0]
        prompt_messages = assemble_imported_code_prompt(
            original_imported_code, generated_code_config
        )
        for index, text in enumerate(params["history"][1:]):
            if index % 2 == 0:
                message: ChatCompletionMessageParam = {
                    "role": "user",
                    "content": text,
                }
            else:
                message: ChatCompletionMessageParam = {
                    "role": "assistant",
                    "content": text,
                }
            prompt_messages.append(message)
    else:
        # Assemble the prompt
        try:
            if params.get("resultImage") and params["resultImage"]:
                prompt_messages = assemble_prompt(
                    params["image"], generated_code_config, params["resultImage"]
                )
            else:
                prompt_messages = assemble_prompt(
                    params["image"], generated_code_config
                )
        except:
            await websocket.send_json(
                {
                    "type": "error",
                    "value": "Error assembling prompt. Contact support at support@picoapps.xyz",
                }
            )
            await websocket.close()
            return

        if params["generationType"] == "update":
            # Transform the history tree into message format
            # TODO: Move this to frontend
            for index, text in enumerate(params["history"]):
                if index % 2 == 0:
                    message: ChatCompletionMessageParam = {
                        "role": "assistant",
                        "content": text,
                    }
                else:
                    message: ChatCompletionMessageParam = {
                        "role": "user",
                        "content": text,
                    }
                prompt_messages.append(message)

            image_cache = create_alt_url_mapping(params["history"][-2])

    # pprint_prompt(prompt_messages)
    completion = ""
    if Config.SHOULD_MOCK_AI_RESPONSE:
        completion = await mock_completion(process_chunk)
    elif Config.IS_MODEL_GEMINI and Config.API_KEY:
        completion = await stream_gemini_response(
                prompt_messages,
                api_key=Config.API_KEY,
                callback=lambda x: process_chunk(x),
            )
    elif Config.API_KEY:
        try:
            completion = await stream_openai_response(
                prompt_messages,
                api_key=Config.API_KEY,
                base_url=openai_base_url,
                callback=lambda x: process_chunk(x),
            )
        except openai.AuthenticationError as e:
            print("[GENERATE_CODE] Authentication failed", e)
            error_message = (
                "Incorrect OpenAI key. Please make sure your OpenAI API key is correct, or create a new OpenAI API key on your OpenAI dashboard."
                + (
                    " Alternatively, you can purchase code generation credits directly on this website."
                    if Config.IS_PROD
                    else ""
                )
            )
            return await throw_error(error_message)
        except openai.NotFoundError as e:
            print("[GENERATE_CODE] Model not found", e)
            error_message = (
                e.message
                + ". Please make sure you have followed the instructions correctly to obtain an OpenAI key with GPT vision access: https://github.com/abi/screenshot-to-code/blob/main/Troubleshooting.md"
                + (
                    " Alternatively, you can purchase code generation credits directly on this website."
                    if Config.IS_PROD
                    else ""
                )
            )
            return await throw_error(error_message)
        except openai.RateLimitError as e:
            print("[GENERATE_CODE] Rate limit exceeded", e)
            error_message = (
                "OpenAI error - 'You exceeded your current quota, please check your plan and billing details.'"
                + (
                    " Alternatively, you can purchase code generation credits directly on this website."
                    if Config.IS_PROD
                    else ""
                )
            )
            return await throw_error(error_message)

    # Write the messages dict into a log so that we can debug later
    write_logs(prompt_messages, completion)

    try:
        if should_generate_images:
            await websocket.send_json(
                {"type": "status", "value": "Generating images..."}
            )
            updated_html = await generate_images(
                completion,
                api_key=openai_api_key,
                base_url=openai_base_url,
                image_cache=image_cache,
            )
        else:
            updated_html = completion
        await websocket.send_json({"type": "setCode", "value": updated_html})
        await websocket.send_json(
            {"type": "status", "value": "Code generation complete."}
        )
    except Exception as e:
        traceback.print_exc()
        print("Image generation failed", e)
        # Send set code even if image generation fails since that triggers
        # the frontend to update history
        await websocket.send_json({"type": "setCode", "value": completion})
        await websocket.send_json(
            {"type": "status", "value": "Image generation failed but code is complete."}
        )
    except RuntimeError as e:
        traceback.print_exc()
        print("Code generation failed", e)
        # Send set code even if image generation fails since that triggers
        # the frontend to update history
        await websocket.send_json({"type": "setCode", "value": completion})
        await websocket.send_json(
            {"type": "status", "value": "Code generation failed but code is complete."}
        )

    await websocket.close()
