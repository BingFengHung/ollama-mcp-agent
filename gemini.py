import argparse
import asyncio
import signal
import sys
import uuid
import warnings
import os  # Added for environment variables
from typing import List, Optional

# --- Environment Variable Setup ---
# Recommended: Use a .env file for local development
# Create a file named .env in the same directory with:
# GOOGLE_API_KEY=your_google_api_key_here
try:
    from dotenv import load_dotenv

    load_dotenv()  # Load environment variables from .env file
except ImportError:
    print("dotenv library not found. Please install it: pip install python-dotenv")
    print("Or ensure GOOGLE_API_KEY is set manually in your environment.")
# ---------------------------------

import nest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages.tool import ToolMessage
from langchain_core.runnables import RunnableConfig
# --- LangChain Google GenAI Import ---
from langchain_google_genai import ChatGoogleGenerativeAI  # Replaced ChatOllama
# -------------------------------------
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.graph import CompiledGraph
from langgraph.prebuilt import create_react_agent

from mcp_manager import cleanup_mcp_client, initialize_mcp_client

# Constants
QUERY_TIMEOUT_SECONDS = 60 * 5
RECURSION_LIMIT = 100
MCP_CHAT_PROMPT = """
    You are a helpful AI assistant that can use tools to answer questions.
    You have access to the following tools:

    {tools}

    Use the following format:

    Question: the input question you must answer
    Thought: you should always think about what to do
    Action: the action to take, should be one of [{tool_names}]
    Action Input: the input to the action
    Observation: the result of the action
    ... (this Thought/Action/Action Input/Observation can repeat N times)
    Thought: I now know the final answer
    Final Answer: the final answer to the original input question

    When using tools, think step by step:
    1. Understand the question and what information is needed.
    2. Look at the available tools ({tool_names}) and their descriptions ({tools}).
    3. Decide which tool, if any, is most appropriate to find the needed information.
    4. Determine the correct input parameters for the chosen tool based on its description.
    5. Call the tool with the determined input.
    6. Analyze the tool's output (Observation).
    7. If the answer is found, formulate the Final Answer. If not, decide if another tool call is needed or if you can answer based on the information gathered.
    8. Only provide the Final Answer once you are certain. Do not use a tool if it's not necessary to answer the question.
    """
# Note: The ReAct prompt above is crucial for create_react_agent.
# You might need to adjust it based on Gemini's behavior.
DEFAULT_SYSTEM_PROMPT = MCP_CHAT_PROMPT  # Using the ReAct specific prompt when tools are present
QUERY_THREAD_ID = str(uuid.uuid4())
DEFAULT_TEMPERATURE = 0.5  # Gemini often works well with slightly lower temps too

# --- Gemini Model ---
# Choose a Gemini model that supports tool calling/function calling
# Common options: "gemini-1.5-flash-latest", "gemini-pro", "gemini-1.5-pro-latest"
GEMINI_MODEL_NAME = "gemini-1.5-flash-latest"


# --------------------


# Signal handler for Ctrl+C on Windows (remains the same)
def handle_sigint(signum, frame):
    print("\n\nProgram terminated. Goodbye!")
    sys.exit(0)


# Create chat model (Updated for Gemini)
def create_chat_model(
        google_api_key: str,  # Added API key parameter
        temperature: float = DEFAULT_TEMPERATURE,
        # streaming: bool = True, # Streaming is handled by LangChain methods like .astream
        system_prompt: Optional[str] = None,  # Will be used by the agent if provided
        mcp_tools: Optional[List] = None,
) -> ChatGoogleGenerativeAI | CompiledGraph:
    if not google_api_key:
        raise ValueError("Google API Key is required to use Gemini.")

    # Create Gemini Chat model
    chat_model = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL_NAME,
        google_api_key=google_api_key,
        temperature=temperature,
    )

    # Create ReAct agent (when MCP tools are available)
    if mcp_tools:
        print("Binding tools to Gemini model using ReAct agent...")
        # Make sure the prompt used here aligns with what create_react_agent expects
        # The MCP_CHAT_PROMPT includes placeholders {tools} and {tool_names}
        # which create_react_agent should fill.
        agent_executor = create_react_agent(
            model=chat_model,
            tools=mcp_tools,
            checkpointer=MemorySaver()
        )
        print("ReAct agent created.")
        return agent_executor  # Return the agent executor graph
    else:
        print("No MCP tools provided. Using plain Gemini model.")
        # If no tools, you might want a simpler system prompt
        # The default behavior without tools might just be the raw chat model.
        # Binding a default System prompt if needed:
        # return SystemMessage(content=system_prompt or "You are a helpful AI assistant.") | chat_model
        return chat_model  # Return the base model if no tools


# Process user input (remains the same)
def process_input(user_input: str) -> Optional[str]:
    cleaned_input = user_input.strip()
    if cleaned_input.lower() in ["quit", "exit", "bye"]:
        return None
    return cleaned_input


# Return streaming callback function and accumulated data
# This function processes the output chunks from LangGraph's streaming
def get_streaming_callback():
    accumulated_text = []
    accumulated_tool_info = []  # Store tool name and response separately

    # Rename 'chunk' parameter to 'data' for clarity, as it receives chunk.ops[0]['value']
    def callback_func(data: any):
        nonlocal accumulated_text, accumulated_tool_info

        if isinstance(data, dict):
            # Try to find the key associated with the agent step containing messages
            agent_step_key = next((k for k in data if isinstance(data.get(k), dict) and 'messages' in data[k]), None)

            if agent_step_key:
                messages = data[agent_step_key].get("messages", [])
                for message in messages:
                    if isinstance(message, AIMessage):
                        # Check if it's an intermediate message (tool call) or final answer chunk
                        if message.tool_calls:
                            # Tool call requested by the model (won't print content yet)
                            pass  # Or log if needed: print(f"DEBUG: Tool call requested: {message.tool_calls}")
                        elif message.content and isinstance(message.content, str):  # Check content is a string
                            # Append and print final response chunks from AIMessage
                            content_chunk = message.content.encode("utf-8", "replace").decode("utf-8")
                            if content_chunk:  # Avoid appending/printing empty strings
                                accumulated_text.append(content_chunk)
                                print(content_chunk, end="", flush=True)

                    elif isinstance(message, ToolMessage):
                        # Result of a tool execution
                        tool_info = f"Tool Used: {message.name}\nResult: {message.content}\n---------------------"
                        print(f"\n[Tool Execution Result: {message.name}]")  # Indicate tool use clearly
                        # print(message.content) # Optionally print the full tool result
                        accumulated_tool_info.append(tool_info)
        return None  # Callback doesn't need to return anything

    return callback_func, accumulated_text, accumulated_tool_info


# Process user query and generate response (Updated error message)
async def process_query(agent, system_prompt, query: str, timeout: int = QUERY_TIMEOUT_SECONDS):
    try:
        # Set up streaming callback
        streaming_callback, accumulated_text, accumulated_tool_info = (
            get_streaming_callback()
        )
        initial_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=query)  # Assuming user_input holds the user's query
        ]

        # Define input for the agent/graph
        inputs = {"messages": initial_messages}

        # Configuration for the graph run
        config = RunnableConfig(
            recursion_limit=RECURSION_LIMIT,
            configurable={"thread_id": QUERY_THREAD_ID},  # Ensure unique thread for stateful execution
            # Add callbacks=[streaming_callback] if astream_log is used
        )

        # Generate response using astream_log for richer streaming data
        # Use astream_graph if that's the specific function you have
        # await astream_graph(...) # Keep your existing call if it works

        # Using astream_log is often standard for LangGraph agents
        async for chunk in agent.astream_log(inputs, config=config, include_types=["llm", "tool"]):
            # The callback function needs to process the structure of these chunks
            # print(f"DEBUG RAW CHUNK: {chunk}") # Debug raw output from astream_log
            streaming_callback(chunk.ops[0]['value'])  # Pass the relevant part to callback

        # Wait for the streaming to complete (astream_log handles this implicitly)
        # await asyncio.wait_for(...) might not be needed if astream_log finishes

        # Return accumulated text and tool info
        full_response = "".join(accumulated_text).strip() if accumulated_text else "AI did not produce a text response."
        tool_info = "\n".join(accumulated_tool_info) if accumulated_tool_info else ""
        return {"output": full_response, "tool_calls": tool_info}

    except asyncio.TimeoutError:
        return {
            "error": f"⏱️ Request exceeded timeout of {timeout} seconds. Please try again."
        }
    except Exception as e:
        import traceback
        print(f"\nDebug info: {traceback.format_exc()}")
        # Provide more specific error feedback if possible
        error_message = f"❌ An error occurred: {str(e)}"
        if "API key not valid" in str(e):
            error_message += "\nPlease check your GOOGLE_API_KEY."
        return {"error": error_message}


async def amain(args):
    """Async main function"""

    mcp_client = None
    google_api_key = os.getenv("GOOGLE_API_KEY")

    if not google_api_key:
        print("\n❌ Error: GOOGLE_API_KEY environment variable not set.")
        print("Please set the GOOGLE_API_KEY environment variable or create a .env file.")
        sys.exit(1)

    try:
        # Initialize MCP client (remains the same)
        print("\n=== Initializing MCP client... ===")
        mcp_client, mcp_tools = await initialize_mcp_client()
        print(f"Loaded {len(mcp_tools)} MCP tools.")

        # Print MCP tool information (remains the same)
        if mcp_tools:
            for tool in mcp_tools:
                print(f"[Tool] {tool.name}: {tool.description}")  # Show description too

        # Initialize Gemini model / agent
        print(f"\n=== Initializing Google Gemini ({GEMINI_MODEL_NAME})... ===")
        chat_model_or_agent = create_chat_model(  # Renamed variable for clarity
            google_api_key=google_api_key,
            temperature=args.temp,
            # system_prompt=args.system_prompt, # Pass system prompt if using agent
            mcp_tools=mcp_tools,
        )

        # Start chat
        print("\n=== Starting Google Gemini Chat ===")  # Updated name
        print("Enter 'quit', 'exit', or 'bye' to exit.")
        print("=" * 40 + "\n")

        # Note: Message history management might need adjustments depending on
        # how the ReAct agent and MemorySaver handle it.
        # For simple MemorySaver, LangGraph handles history via thread_id.
        # We don't need to manually manage `message_history` list for the agent call itself.

        while True:
            try:
                # Get user input
                user_input = input("\nUser: ")

                # Process input
                processed_input = process_input(user_input)
                if processed_input is None:
                    print("\nChat ended. Goodbye!")
                    break

                # Generate response
                print("AI:\n", end="", flush=True)

                # Pass the agent/model to process_query
                response = await process_query(
                    chat_model_or_agent, args.system_prompt or DEFAULT_SYSTEM_PROMPT, processed_input,
                    timeout=int(args.timeout)
                )

                # The streaming callback now handles printing the AI response chunks
                print()  # Add a newline after the streaming output is complete

                if "error" in response:
                    print(f"\nError: {response['error']}")
                    continue

                # Display tool call information (optional)
                if (
                        args.show_tools
                        and "tool_calls" in response
                        and response["tool_calls"].strip()
                ):
                    print("\n--- Tool Activity ---")
                    print(response["tool_calls"].strip())
                    print("---------------------\n")

                # History is managed by LangGraph's MemorySaver via thread_id

            except KeyboardInterrupt:
                # handle_sigint will catch this if triggered during input()
                # This catches it if during await process_query
                print("\n\nProgram terminated by user. Goodbye!")
                break
            except Exception as e:
                print(f"\nAn unexpected error occurred in the main loop: {str(e)}")
                import traceback
                print(traceback.format_exc())  # Print detailed traceback for debugging
                continue  # Continue the loop if possible

    except Exception as e:
        print(f"\n\nAn critical error occurred during setup or execution: {str(e)}")
        import traceback
        print(traceback.format_exc())
        # No raise here, allow finally block to run
    finally:
        # Clean up MCP client (remains the same)
        if mcp_client is not None:
            print("\nCleaning up MCP client...")
            await cleanup_mcp_client(mcp_client)
            print("MCP client cleanup complete.")


def main():
    """Main function"""
    # Setup signal handler early
    signal.signal(signal.SIGINT, handle_sigint)

    # Warning filter settings (remains the same)
    warnings.filterwarnings(
        "ignore", category=ResourceWarning, message="unclosed.*<socket.socket.*>"
    )

    loop = None  # Initialize loop variable
    try:
        # Parse command line arguments
        parser = argparse.ArgumentParser(description="Google Gemini Chat CLI with MCP Tools")  # Updated description
        parser.add_argument(
            "--temp",
            type=float,
            default=DEFAULT_TEMPERATURE,
            help=f"Temperature value (0.0 ~ 1.0). Default: {DEFAULT_TEMPERATURE}",
        )
        # --no-stream argument might be less relevant as streaming is default/preferred
        # parser.add_argument("--no-stream", action="store_true", help="Disable streaming (May not be fully supported)")
        parser.add_argument(
            "--system-prompt", type=str, default=None,  # Default to None, let create_chat_model handle defaults
            help="Custom base system prompt (Note: ReAct agent uses a specific format)"
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=QUERY_TIMEOUT_SECONDS,
            help=f"Response generation timeout (seconds). Default: {QUERY_TIMEOUT_SECONDS}",
        )
        parser.add_argument(
            "--show-tools", action="store_true", help="Show tool execution information"
        )
        args = parser.parse_args()

        # Validate temperature
        if not 0.0 <= args.temp <= 1.0:
            print(
                f"Warning: Temperature {args.temp} is outside the typical range [0.0, 1.0]. Using default: {DEFAULT_TEMPERATURE}")
            args.temp = DEFAULT_TEMPERATURE

        nest_asyncio.apply()
        asyncio.run(amain(args))

    except SystemExit:
        # Raised by sys.exit(), like in handle_sigint or API key check
        print("Exiting program.")
    except Exception as e:
        print(f"\n\nAn error occurred during program execution: {str(e)}")
        import traceback
        print(traceback.format_exc())
        sys.exit(1)  # Exit with error code


if __name__ == "__main__":
    main()
