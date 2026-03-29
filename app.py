import os
import sys
import subprocess
import re
import time
import json
import concurrent.futures
import uuid
import shutil
from gradio_client import Client
from datetime import datetime

# Startup tools setup
def setup_tools():
    # 1. Patch TinyTroupe
    if os.path.exists("external/TinyTroupe"):
        patch_tinytroupe()

    # 2. Setup md2googleslides
    gslides_path = "external/md2googleslides"
    if not os.path.exists(gslides_path):
        print("Cloning md2googleslides...")
        subprocess.run(["git", "clone", "https://github.com/googleworkspace/md2googleslides.git", gslides_path])

    if os.path.exists(gslides_path):
        if not os.path.exists(os.path.join(gslides_path, "node_modules")):
            print("Installing md2googleslides dependencies...")
            subprocess.run(["npm", "install"], cwd=gslides_path)
            print("Compiling md2googleslides...")
            subprocess.run(["npm", "run", "compile"], cwd=gslides_path)

def patch_tinytroupe():
    path = "external/TinyTroupe/tinytroupe/openai_utils.py"
    if os.path.exists(path):
        with open(path, "r") as f:
            content = f.read()

        # 1. Import concurrent.futures and add parallel helper to the class
        if "import concurrent.futures" not in content:
            content = "import concurrent.futures\n" + content

        # Add the parallel helper to OpenAIClient
        parallel_helper = """
    def _raw_model_call_parallel(self, model_names, chat_api_params):
        def make_call(m_name):
            try:
                p = chat_api_params.copy()
                p["model"] = m_name
                # Adjust for reasoning models if needed
                if self._is_reasoning_model(m_name):
                    if "max_tokens" in p:
                        p["max_completion_tokens"] = p.pop("max_tokens")
                    p.pop("temperature", None)
                    p.pop("top_p", None)
                    p.pop("frequency_penalty", None)
                    p.pop("presence_penalty", None)
                    p.pop("stream", None)

                return self.client.chat.completions.create(**p)
            except Exception as e:
                return e

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(model_names)) as executor:
            futures = {executor.submit(make_call, m): m for m in model_names}
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if not isinstance(res, Exception):
                    return res
        return Exception("All parallel calls failed")
"""
        if "_raw_model_call_parallel" not in content:
            content = content.replace("class OpenAIClient:", "class OpenAIClient:" + parallel_helper)

        # 2. Ensure alias-huge is used (alias-large is deprecated/down)
        content = content.replace('"alias-fast"', '"alias-huge"')
        content = content.replace('"alias-large"', '"alias-huge"')

        # 3. Handle 502 errors by waiting 35 seconds and setting a parallel retry flag
        # We need to modify the send_message loop

        # Inject parallel_retry = False before the loop
        content = content.replace("i = 0", "parallel_retry = False\n        i = 0")

        # Modify the model call inside the loop
        if 'if parallel_retry:' not in content:
            old_call = "response = self._raw_model_call(model, chat_api_params)"
            new_call = """if parallel_retry:
                        logger.info("Attempting parallel call to alias-huge and alias-fast.")
                        response = self._raw_model_call_parallel(["alias-huge", "alias-fast"], chat_api_params)
                        if isinstance(response, Exception):
                            raise response
                    else:
                        response = self._raw_model_call(model, chat_api_params)"""
            content = content.replace(old_call, new_call)

        # Update the 502 catch block
        pattern = r"if isinstance\(e, openai\.APIStatusError\) and e\.status_code == 502 and isinstance\(self, HelmholtzBlabladorClient\):.*?except Exception as fallback_e:.*?logger\.error\(f\"Fallback to OpenAI also failed: \{fallback_e\}\"\)"

        new_502_block = """if isinstance(e, openai.APIStatusError) and e.status_code == 502 and isinstance(self, HelmholtzBlabladorClient):
                    logger.warning("Helmholtz API returned a 502 error. Waiting 35 seconds and enabling parallel retry...")
                    parallel_retry = True
                    time.sleep(35)"""

        content = re.sub(pattern, new_502_block, content, flags=re.DOTALL)

        with open(path, "w") as f:
            f.write(content)
        print("TinyTroupe patched to handle 502 errors with 35s wait and parallel retries.")

# Run setup
setup_tools()

import gradio as gr
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from github import Github, Auth
import requests
from openai import OpenAI
import logging

# Add external/TinyTroupe to sys.path
TINYTROUPE_PATH = os.path.join(os.getcwd(), "external", "TinyTroupe")
sys.path.append(TINYTROUPE_PATH)

# Try to import tinytroupe
try:
    import tinytroupe
    from tinytroupe.agent import TinyPerson
    from tinytroupe.factory.tiny_person_factory import TinyPersonFactory
    from tinytroupe import config_manager
    print("TinyTroupe imported successfully")
except ImportError as e:
    print(f"Error importing TinyTroupe: {e}")

# Configuration from environment variables
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GITHUB_API_KEY")
ANALYSIS_API_KEY = os.environ.get("ANALYSIS_API_KEY") or os.environ.get("JULES_API_KEY")
BLABLADOR_API_KEY = os.environ.get("BLABLADOR_API_KEY")
BLABLADOR_BASE_URL = "https://api.helmholtz-blablador.fz-juelich.de/v1"
ANALYSIS_API_URL = "https://jules.googleapis.com/v1alpha"

# GitHub Client
gh = Github(auth=Auth.Token(GITHUB_TOKEN)) if GITHUB_TOKEN else None
REPO_NAME = "JsonLord/tiny_web"
POOL_REPO_NAME = "JsonLord/agent-notes"
POOL_PATH = "PersonaPool"

# Better summaries for example personas
BETTER_SUMMARIES = {
    "Friedrich_Wolf.agent.json": "A meticulous German architect at Awesome Inc. He focuses on standardizing apartment designs, favoring quality over cost, and can be confrontational when challenged.",
    "Lila.agent.json": "A freelance linguist from Paris specializing in NLP. She is highly analytical, creative, and excels at anticipating user behavior from ambiguous data.",
    "Oscar.agent.json": "A German architect at Awesome Inc. who balances professional excellence with a witty sense of humor. He is detail-oriented and dedicated to sustainable design.",
    "Sophie_Lefevre.agent.json": "A creative professional likely focused on the aesthetic and emotional aspects of design and user experience.",
    "Marcos.agent.json": "A technically-minded individual who prioritizes efficiency and robust, logical solutions in the products he uses.",
    "Lisa.agent.json": "A standard user persona interested in efficiency and clear communication.",
    "Jane_Smith.md": "Standard, versatile persona representing a broad range of consumer behaviors and expectations.",
    "John_Doe.md": "Standard, versatile persona representing a broad range of consumer behaviors and expectations."
}

# Global state for processed reports
processed_prs = set()
all_discovered_reports = ""
github_logs = []

# Slide rendering configuration
SLIDES_OUTPUT_ROOT = os.path.join(os.getcwd(), "rendered_slides_output")
os.makedirs(SLIDES_OUTPUT_ROOT, exist_ok=True)

def add_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    github_logs.append(log_entry)
    print(log_entry)
    return "\n".join(github_logs[-20:])

# Helper for parallel LLM calls
def call_llm_parallel(client, model_names, messages, **kwargs):
    def make_call(model_name):
        try:
            print(f"Parallel call attempting: {model_name}")
            return client.chat.completions.create(
                model=model_name,
                messages=messages,
                **kwargs
            )
        except Exception as e:
            print(f"Parallel call error from {model_name}: {e}")
            return e

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(model_names)) as executor:
        futures = {executor.submit(make_call, m): m for m in model_names}
        # Wait for the first success that isn't a 502/Proxy Error
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if not isinstance(res, Exception):
                print(f"Parallel call success from: {futures[future]}")
                # Try to cancel others (not always possible but good practice)
                return res
            else:
                # If it's an error, check if we should keep waiting or if all failed
                pass

    return Exception("All parallel calls failed")

# BLABLADOR Client for task generation
def get_blablador_client():
    if not BLABLADOR_API_KEY:
        return None
    return OpenAI(
        api_key=BLABLADOR_API_KEY,
        base_url=BLABLADOR_BASE_URL
    )

def get_user_repos(github_client=None):
    client = github_client or gh
    add_log("Fetching user repositories...")
    if not client:
        add_log("ERROR: GitHub client not initialized.")
        return ["JsonLord/tiny_web"]
    try:
        user = client.get_user()
        repos = [repo.full_name for repo in user.get_repos()]
        add_log(f"Found {len(repos)} repositories.")
        if "JsonLord/tiny_web" not in repos:
            repos.append("JsonLord/tiny_web")
        return sorted(repos)
    except Exception as e:
        add_log(f"ERROR fetching repos: {e}")
        return ["JsonLord/tiny_web"]

def get_repo_branches(repo_full_name, github_client=None):
    client = github_client or gh
    add_log(f"Fetching branches for {repo_full_name}...")
    if not client:
        add_log("ERROR: GitHub client is None.")
        return ["main"]
    if not repo_full_name:
        return ["main"]
    try:
        repo = client.get_repo(repo_full_name)
        # Fetch branches
        branches = list(repo.get_branches())
        add_log(f"Discovered {len(branches)} branches.")
        
        # Use ThreadPool to fetch commit dates in parallel to be MUCH faster
        branch_info = []
        
        def fetch_branch_date(b):
            try:
                commit = repo.get_commit(b.commit.sha)
                # Try multiple ways to get the date
                date = None
                if commit.commit and commit.commit.author:
                    date = commit.commit.author.date
                elif commit.commit and commit.commit.committer:
                    date = commit.commit.committer.date
                
                if not date:
                    date = datetime.min
                return (b.name, date)
            except Exception as e:
                return (b.name, datetime.min)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            branch_info = list(executor.map(fetch_branch_date, branches))
        
        # Sort by date descending
        branch_info.sort(key=lambda x: x[1], reverse=True)
        result = [b[0] for b in branch_info]
        
        if result:
            add_log(f"Successfully sorted {len(result)} branches. Latest: {result[0]}")
        
        return result
    except Exception as e:
        add_log(f"ERROR fetching branches: {e}")
        import traceback
        traceback.print_exc()
        return ["main"]

def get_persona_pool():
    if not gh:
        return []
    try:
        repo = gh.get_repo(POOL_REPO_NAME)
        contents = repo.get_contents(POOL_PATH)
        pool = []
        for content_file in contents:
            if content_file.name.endswith(".json"):
                file_content = content_file.decoded_content.decode("utf-8")
                pool.append(json.loads(file_content))
        return pool
    except Exception as e:
        print(f"Error fetching persona pool: {e}")
        return []

def get_example_personas():
    example_path = "external/TinyTroupe/examples/agents/"
    if not os.path.exists(example_path):
        return []
    try:
        files = [f for f in os.listdir(example_path) if f.endswith(".json") or f.endswith(".md")]
        return sorted(files)
    except Exception as e:
        print(f"Error listing example personas: {e}")
        return []

def upload_persona_to_pool(persona_data):
    if not gh:
        return
    try:
        repo = gh.get_repo(POOL_REPO_NAME)
        name = persona_data.get("name", "unknown").replace(" ", "_")
        file_path = f"{POOL_PATH}/{name}.json"
        content = json.dumps(persona_data, indent=4)

        try:
            # Check if file exists to get its sha
            existing_file = repo.get_contents(file_path)
            repo.update_file(file_path, f"Update persona: {name}", content, existing_file.sha)
        except:
            # Create new file
            repo.create_file(file_path, f"Add persona: {name}", content)
        print(f"Uploaded persona {name} to pool.")
    except Exception as e:
        print(f"Error uploading persona to pool: {e}")

def select_or_create_personas(theme, customer_profile, num_personas, force_method=None, example_file=None):
    if force_method == "Example Persona" and example_file:
        add_log(f"Loading example persona from {example_file}...")
        try:
            path = os.path.join("external/TinyTroupe/examples/agents/", example_file)
            if example_file.endswith(".json"):
                with open(path, "r") as f:
                    data = json.load(f)

                name = data.get("name") or data.get("persona", {}).get("name") or "Unknown"
                bio = BETTER_SUMMARIES.get(example_file)
                if not bio:
                    bio = data.get("mental_faculties", [{}])[0].get("context") if "mental_faculties" in data else "An example persona."

                # Adapt TinyTroupe format to our internal format
                persona = {
                    "name": name,
                    "minibio": bio,
                    "persona": data
                }
            else: # .md
                with open(path, "r") as f:
                    content = f.read()
                name = example_file.replace(".md", "").replace("_", " ")
                bio = BETTER_SUMMARIES.get(example_file) or content
                persona = {
                    "name": name,
                    "minibio": bio,
                    "persona": {"name": name, "background": content}
                }
            return [persona] * int(num_personas)
        except Exception as e:
            add_log(f"Failed to load example persona: {e}")

    if force_method == "DeepPersona":
        add_log("Forcing DeepPersona generation...")
        personas = []
        for i in range(int(num_personas)):
            p = generate_persona_from_deeppersona(theme, customer_profile)
            if p: personas.append(p)
        if len(personas) >= int(num_personas): return personas[:int(num_personas)]
        # fallback if some failed
        num_personas = int(num_personas) - len(personas)
    elif force_method == "TinyTroupe":
        add_log("Forcing TinyTroupe generation...")
        return generate_personas_from_tiny_factory(theme, customer_profile, num_personas)

    client = get_blablador_client()
    if not client:
        return generate_personas(theme, customer_profile, num_personas)

    pool = get_persona_pool()
    if not pool:
        print("Pool is empty, generating new personas.")
        new_personas = generate_personas(theme, customer_profile, num_personas)
        for p in new_personas:
            upload_persona_to_pool(p)
        return new_personas

    # Ask LLM to judge
    pool_summaries = [{"index": i, "name": p["name"], "minibio": p.get("minibio", "")} for i, p in enumerate(pool)]

    prompt = f"""
    You are an expert in user experience research and persona management.
    We need {num_personas} persona(s) for a UX analysis task with the following theme: {theme}
    And target customer profile: {customer_profile}

    Here is a pool of existing personas:
    {json.dumps(pool_summaries, indent=2)}

    For each of the {num_personas} required personas, decide if one from the pool is an appropriate match or if we should create a new one.
    An appropriate match is a persona whose background, interests, and characteristics align well with the target customer profile and theme.

    Return your decision as a JSON object with the following format:
    {{
        "decisions": [
            {{ "action": "use_pool", "pool_index": 0 }},
            {{ "action": "create_new" }},
            ... (up to {num_personas})
        ]
    }}
    """

    try:
        response = client.chat.completions.create(
            model="alias-huge",
            messages=[{"role": "user", "content": prompt}]
        )
        content = response.choices[0].message.content
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            decisions_json = json.loads(json_match.group())
            decisions = decisions_json.get("decisions", [])
        else:
            print("Could not parse LLM decision, creating new personas.")
            decisions = [{"action": "create_new"}] * num_personas
    except Exception as e:
        print(f"Error getting LLM decision: {e}, creating new personas.")
        decisions = [{"action": "create_new"}] * num_personas

    final_personas = []
    to_create_count = 0
    for d in decisions:
        if d["action"] == "use_pool" and 0 <= d["pool_index"] < len(pool):
            print(f"Using persona from pool: {pool[d['pool_index']]['name']}")
            final_personas.append(pool[d['pool_index']])
        else:
            to_create_count += 1

    if to_create_count > 0:
        print(f"Creating {to_create_count} new personas.")
        newly_created = generate_personas(theme, customer_profile, to_create_count)
        for p in newly_created:
            upload_persona_to_pool(p)
            final_personas.append(p)

    return final_personas

def generate_persona_from_deeppersona(theme, customer_profile):
    add_log("Attempting persona generation from THzva/deeppersona-experience...")
    client = get_blablador_client()
    if not client:
        return None

    # Step 1: Breakdown profile into parameters using LLM alias-huge
    prompt = f"""
    You are an expert in persona creation.
    Break down the following business theme and customer profile into detailed attributes for a persona.
    Business Theme: {theme}
    Target Customer Profile: {customer_profile}

    Return a JSON object with exactly these fields:
    - age (int)
    - gender (string)
    - occupation (string)
    - city (string)
    - country (string)
    - custom_values (string, e.g., "Sustainability, Innovation")
    - custom_life_attitude (string, e.g., "Optimistic and forward-thinking")
    - life_story (string, a brief background)
    - interests_hobbies (string, comma separated)
    - name (string, full name)

    CRITICAL: Return ONLY the JSON object.
    """

    try:
        response = client.chat.completions.create(
            model="alias-huge",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        params = json.loads(response.choices[0].message.content)
        add_log(f"Profile breakdown complete for {params.get('name')}")

        # Step 2: Call the DeepPersona generation endpoint
        gr_client = Client("THzva/deeppersona-experience")
        result = gr_client.predict(
                age=float(params.get("age", 30)),
                gender=params.get("gender", "Unknown"),
                occupation=params.get("occupation", theme),
                city=params.get("city", "Unknown"),
                country=params.get("country", "Unknown"),
                custom_values=params.get("custom_values", "Efficiency"),
                custom_life_attitude=params.get("custom_life_attitude", "Neutral"),
                life_story=params.get("life_story", "A brief life story."),
                interests_hobbies=params.get("interests_hobbies", "None"),
                attribute_count=200,
                api_name="/generate_persona"
        )

        name = params.get("name")
        if not name:
            name_match = re.search(r"I am ([^,\.]+)", result)
            name = name_match.group(1) if name_match else f"User_{uuid.uuid4().hex[:4]}"
        
        return {
            "name": name,
            "minibio": result,
            "persona": params
        }
    except Exception as e:
        add_log(f"DeepPersona generation failed: {e}")
        return None

def generate_personas_from_tiny_factory(theme, customer_profile, num_personas):
    add_log(f"Generating {num_personas} personas from harvesthealth/tiny_factory...")
    try:
        gr_client = Client("harvesthealth/tiny_factory")
        result = gr_client.predict(
            business_description=theme,
            customer_profile=customer_profile,
            num_personas=float(num_personas),
            blablador_api_key=BLABLADOR_API_KEY,
            api_name="/generate_personas"
        )
        # Assuming the result is a list of personas in the format we need
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and "personas" in result:
            return result["personas"]
        else:
            add_log(f"Unexpected format from tiny_factory: {type(result)}")
            # If it's a string, maybe it's JSON?
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except:
                    pass
            return []
    except Exception as e:
        add_log(f"Tiny Factory generation failed: {e}")
        return []

def generate_personas(theme, customer_profile, num_personas):
    add_log(f"Generating {num_personas} personas...")
    
    # Try Tiny Factory first
    final_personas = generate_personas_from_tiny_factory(theme, customer_profile, num_personas)
    if len(final_personas) >= int(num_personas):
        add_log("Successfully generated all personas from Tiny Factory.")
        return final_personas[:int(num_personas)]
    
    add_log("Falling back to TinyTroupe logic for remaining personas...")
    
    # Ensure alias-huge is used
    config_manager.update("model", "alias-huge")
    config_manager.update("reasoning_model", "alias-huge")

    context = f"A company related to {theme}. Target customers: {customer_profile}"

    # Manually define sampling plan if LLM fails to generate one correctly
    try:
        factory = TinyPersonFactory(context=context)
        # Attempt to initialize sampling plan, if it fails or produces 0 samples, we'll manually add one
        try:
            factory.initialize_sampling_plan()
        except:
            pass

        if not factory.remaining_characteristics_sample or any("sampled_values" not in s for s in factory.remaining_characteristics_sample):
            print("Sampling plan generation failed or returned invalid samples. Creating manual sample.")
            factory.remaining_characteristics_sample = [{
                "name": f"User_{i}",
                "age": 25 + i,
                "gender": "unknown",
                "nationality": "unknown",
                "occupation": theme,
                "background": customer_profile
            } for i in range(int(num_personas))]
        else:
            # If it has sampled_values but it's nested (it should be flattened by factory)
            # Actually, the error shows it's a list of dictionaries that might be errors
            pass

        people = factory.generate_people(number_of_people=int(num_personas) - len(final_personas), verbose=True)
        if not people:
            print("TinyTroupe generated 0 people. Using fallback.")
            raise Exception("No people generated.")
    except Exception as e:
        print(f"Error in generate_personas: {e}")
        # Fallback: create dummy people if everything fails
        personas_data = []
        for i in range(int(num_personas) - len(final_personas)):
            idx = len(final_personas) + i
            personas_data.append({
                "name": f"User_{idx}",
                "minibio": f"A simulated user interested in {theme}.",
                "persona": {"name": f"User_{idx}", "occupation": theme, "background": customer_profile}
            })
        return personas_data

    personas_data = final_personas
    if people:
        for person in people:
            personas_data.append({
                "name": person.name,
                "minibio": person.minibio(),
                "persona": person._persona
            })
    return personas_data

def generate_tasks(theme, customer_profile, url, discovery_text="", language="English"):
    client = get_blablador_client()
    if not client:
        return [f"Task {i+1} for {theme} (BLABLADOR_API_KEY not set)" for i in range(5)]

    prompt = f"""
    You are a UX Strategic Discovery Agent.

    Target Website: {url}
    Strategic Theme: {theme}
    Persona Profile: {customer_profile}
    Language: {language}

    Discovery Context from Website:
    ---
    {discovery_text}
    ---

    Generate EXACTLY 5 sequential tasks for a user to perform on the website: {url}.
    The tasks must be written in {language}.

    The tasks should cover:
    1. Communication
    2. Purchase decisions
    3. Custom Search / Information gathering
    4. Emotional connection to the persona and content/styling

    The tasks must be in sequential order and specific to the website {url}.

    CRITICAL: Skip all internal monologue or thinking process. Return ONLY a JSON object with a "tasks" key containing a list of exactly 5 strings.
    Example: {{"tasks": ["task 1", "task 2", "task 3", "task 4", "task 5"]}}
    Do not include any other text in your response.
    """

    models_to_try = ["alias-huge", "alias-fast", "alias-large"]

    for attempt in range(5):
        try:
            print(f"Attempt {attempt+1} for task generation...")
            if attempt > 0:
                print(f"Retrying in parallel with {models_to_try}")
                # Wait 35s if it's a retry (likely Proxy Error or Rate Limit)
                time.sleep(35)
                response = call_llm_parallel(client, models_to_try, [{"role": "user", "content": prompt}], response_format={"type": "json_object"})
            else:
                response = client.chat.completions.create(
                    model="alias-huge",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )

            if response and not isinstance(response, Exception):
                content = response.choices[0].message.content

                # Explicitly strip <think> blocks if they are present
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

                # Robust extraction
                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    try:
                        tasks_json = json.loads(json_match.group())
                        tasks = tasks_json.get("tasks", [])
                        if tasks and isinstance(tasks, list) and len(tasks) >= 3:
                            return tasks[:5]
                    except:
                        pass

                # Fallback: try to extract lines that look like tasks
                lines = [re.sub(r'^\d+[\.\)]\s*', '', l).strip() for l in content.split('\n') if l.strip()]
                tasks = [l for l in lines if len(l) > 20 and not l.startswith('{') and not l.startswith('`')]
                if len(tasks) >= 3:
                    return tasks[:5]

            print(f"Attempt {attempt+1} failed to yield valid tasks.")
        except Exception as e:
            print(f"Error in attempt {attempt+1}: {e}")

    return [f"Task {i+1} for {theme} (Manual fallback)" for i in range(5)]

def handle_generate(theme, customer_profile, num_personas, method, example_file, url, language):
    try:
        current_profile = customer_profile
        discovery_text = ""

        yield "Starting Strategic Discovery (Phase 1)...", None, None, None
        try:
            browser_client = Client("diamond-in/Browser-Use-mcp")
            discovery_text = browser_client.predict(
                url=url,
                selector="body",
                use_persistent=False,
                api_name="/browse_and_extract"
            )
            add_log("Strategic Discovery complete.")
        except Exception as e:
            add_log(f"Strategic Discovery failed: {e}. Proceeding without discovery context.")

        if method == "Example Persona" and example_file:
            # Fetch example persona info to use as profile context for task generation
            ex_personas = select_or_create_personas("", "", 1, "Example Persona", example_file)
            if ex_personas:
                current_profile = ex_personas[0].get('minibio', customer_profile)

        yield "Thinking...", None, None, None
        tasks = generate_tasks(theme, current_profile, url, discovery_text=discovery_text, language=language)
        tasks_text = "\n".join(tasks) if isinstance(tasks, list) else str(tasks)

        yield "Selecting or creating personas...", tasks_text, None, tasks
        personas = select_or_create_personas(theme, customer_profile, num_personas, force_method=method, example_file=example_file)

        yield "Generation complete!", tasks_text, personas, tasks
    except Exception as e:
        yield f"Error during generation: {str(e)}", None, None, None

def check_branch_exists(repo_full_name, branch_name):
    if not gh: return False
    try:
        repo = gh.get_repo(repo_full_name)
        repo.get_branch(branch_name)
        return True
    except:
        return False

def download_session_id_file(session_id):
    if not session_id:
        return None
    file_path = f"session_id_{session_id}.txt"
    with open(file_path, "w") as f:
        f.write(session_id)
    return file_path

def get_merged_slides_markdown(repo_full_name, branch_name, report_path):
    if not gh: return None
    repo = gh.get_repo(repo_full_name)
    is_slides_dir = report_path.endswith("/slides") or report_path.endswith("/slides/") or "user_experience_reports/slides" in report_path

    if is_slides_dir:
        try:
            folder_contents = repo.get_contents("user_experience_reports/slides", ref=branch_name)
            slide_files = [c for c in folder_contents if c.name.endswith(".md")]
            slide_files.sort(key=lambda x: x.name)
            merged_content = ""
            for i, sf in enumerate(slide_files):
                file_data = repo.get_contents(sf.path, ref=branch_name)
                slide_text = file_data.decoded_content.decode("utf-8")
                if i > 0: merged_content += "\n\n---\n\n"
                merged_content += slide_text
            return merged_content
        except:
            pass

    try:
        file_content = repo.get_contents(report_path, ref=branch_name)
        return file_content.decoded_content.decode("utf-8")
    except:
        return None

def export_to_google_slides(session_id):
    if not session_id:
        return "Please enter a Session ID first."

    add_log(f"Exporting slides for session {session_id} to Google Slides...")

    # 1. Resolve branch
    branches = get_repo_branches(REPO_NAME)
    latest_branch = resolve_branch(REPO_NAME, session_id, branches)
    if not latest_branch:
        return f"❌ Error: Branch matching '{session_id}' not found."

    # 2. Find slides content
    slides_options = get_reports_in_branch(REPO_NAME, latest_branch, filter_type="slides")
    if not slides_options:
        return "❌ No slides found for this session. Ensure analysis is complete."

    target_path = slides_options[0]
    md_content = get_merged_slides_markdown(REPO_NAME, latest_branch, target_path)
    if not md_content:
        return "❌ Error: Could not retrieve slides Markdown content."

    # 3. Configure Auth
    home = os.path.expanduser("~")
    config_dir = os.path.join(home, ".md2googleslides")
    os.makedirs(config_dir, exist_ok=True)

    client_id_json = os.environ.get("GOOGLE_CLIENT_ID_JSON")
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

    if not client_id_json or not credentials_json:
        return "❌ Error: GOOGLE_CLIENT_ID_JSON or GOOGLE_CREDENTIALS_JSON environment variables not set in secrets."

    try:
        with open(os.path.join(config_dir, "client_id.json"), "w") as f:
            f.write(client_id_json)
        with open(os.path.join(config_dir, "credentials.json"), "w") as f:
            f.write(credentials_json)
    except Exception as e:
        return f"❌ Error configuring Google Auth: {e}"

    # 4. Save MD to temp file
    temp_md = f"temp_slides_{session_id}.md"
    try:
        with open(temp_md, "w") as f:
            f.write(md_content)

        # 5. Run tool
        gslides_bin = os.path.abspath("external/md2googleslides/bin/md2gslides.js")
        cmd = ["node", gslides_bin, os.path.abspath(temp_md), "--no-browser", "--title", f"UX Analysis: {session_id}"]

        # Set environment to include node_modules
        env = os.environ.copy()
        env["NODE_PATH"] = os.path.abspath("external/md2googleslides/node_modules")

        result = subprocess.run(cmd, capture_output=True, text=True, cwd="external/md2googleslides", env=env)

        # 6. Parse output for URL
        output = result.stdout + result.stderr
        url_match = re.search(r"(https://docs\.google\.com/presentation/d/[a-zA-Z0-9_-]+)", output)
        if url_match:
            presentation_url = url_match.group(1)
            add_log(f"Successfully generated Google Slides: {presentation_url}")
            return f"### ✅ Slides generated!\n\nAccess and copy your presentation here:\n{presentation_url}"
        else:
            add_log(f"md2gslides failed. Output: {output}")
            return f"❌ Failed to generate slides. Output summary:\n```\n{output[-500:]}\n```"
    except Exception as e:
        add_log(f"Error executing md2gslides: {e}")
        return f"❌ Error executing md2gslides: {str(e)}"
    finally:
        if os.path.exists(temp_md):
            os.remove(temp_md)

def find_jules_session_by_id(search_term):
    if not ANALYSIS_API_KEY or not search_term:
        return None

    headers = {
        "X-Goog-Api-Key": ANALYSIS_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        add_log(f"Searching for Jules session matching: {search_term}...")
        response = requests.get(f"{ANALYSIS_API_URL}/sessions", headers=headers)
        if response.status_code == 200:
            sessions = response.json().get("sessions", [])
            for s in sessions:
                title = s.get("title", "")

                # Case 1: Search term is exactly the UUID (sessions/xxx)
                if search_term == s.get("id") or search_term == s.get("name"):
                    return s.get("name")

                # Case 2: Direct containment
                if f"({search_term})" in title or search_term in title:
                    name = s.get("name")
                    add_log(f"Found Jules session (Direct Match): {name} (Title: {title})")
                    return name

                # Case 3: Fuzzy match - extract ID from title parentheses
                match = re.search(r'\(([^)]+)\)', title)
                if match:
                    id_in_title = match.group(1)
                    if id_in_title.lower() in search_term.lower() or search_term.lower() in id_in_title.lower():
                        name = s.get("name")
                        add_log(f"Found Jules session (Fuzzy Match): {name} (Title: {title})")
                        return name
        return None
    except Exception as e:
        print(f"Error finding Jules session: {e}")
        return None

def resolve_branch(repo_name, session_id, branches):
    if not session_id:
        return branches[0] if branches else "main"

    # Try exact match
    if session_id in branches:
        return session_id

    # Try case-insensitive exact match
    for b in branches:
        if b.lower() == session_id.lower():
            return b

    # Try fuzzy match (contains)
    matches = [b for b in branches if session_id.lower() in b.lower()]
    if matches:
        return matches[0] # Most recent match since branches are sorted by date

    return None

def start_and_monitor_sessions(personas, tasks, url, session_id, language):
    repo_name = REPO_NAME

    # Ticketing system: Session ID is used as the branch name for analysis
    if not session_id:
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        add_log(f"Auto-generated Session ID (Branch): {session_id}")

    # For starting analysis, we don't strictly require the branch to exist yet
    # as Jules might create it or we might be starting on main.
    if not check_branch_exists(repo_name, session_id):
        add_log(f"Warning: Branch '{session_id}' not found on GitHub. Proceeding with analysis (Jules may create it).")

    if not personas or not tasks:
        yield "Error: Personas or Tasks missing. Please generate them first.", "", "", ""
        return

    if not ANALYSIS_API_KEY:
        yield "Error: Analysis API key not set.", "", "", ""
        return

    with open("analysis_template.md", "r") as f:
        template = f.read()

    sessions = []
    jules_uuids = []

    for persona in personas:
        # Use provided session_id or append to it if multiple personas?
        # For simplicity, we use session_id as the report_id too
        report_id = session_id
        
        # Format prompt
        prompt = template.replace("{{persona_context}}", json.dumps(persona))
        prompt = prompt.replace("{{persona_language}}", language)
        prompt = prompt.replace("{{tasks_list}}", json.dumps(tasks))
        prompt = prompt.replace("{{url}}", url)
        prompt = prompt.replace("{{report_id}}", report_id)
        prompt = prompt.replace("{{blablador_api_key}}", BLABLADOR_API_KEY if BLABLADOR_API_KEY else "YOUR_API_KEY")

        # Call Analysis API
        headers = {
            "X-Goog-Api-Key": ANALYSIS_API_KEY,
            "Content-Type": "application/json"
        }
        data = {
            "prompt": prompt,
            "sourceContext": {
                "source": f"sources/github/{repo_name}",
                "githubRepoContext": {
                    "startingBranch": "agent-0-integration"
                }
            },
            "automationMode": "AUTO_CREATE_PR",
            "title": f"UX Analysis for {persona['name']} ({session_id})"
        }

        response = requests.post(f"{ANALYSIS_API_URL}/sessions", headers=headers, json=data)
        if response.status_code == 200:
            sess_data = response.json()
            sessions.append(sess_data)
            jules_uuids.append(sess_data['id'])
            # Yield session ID immediately so UI can update. 3rd output is Branch Name, 4th is Jules UUID
            yield f"Session created: {sess_data['id']}. ID: {session_id}", "", session_id, sess_data['id']
        else:
            yield f"Error creating session for {persona['name']}: {response.text}", "", "", ""
            return

    # Monitoring
    all_reports = ""
    last_jules_uuid = jules_uuids[-1] if jules_uuids else ""
    while sessions:
        for i, session in enumerate(sessions):
            curr_jules_uuid = session['id']
            last_jules_uuid = curr_jules_uuid
            res = requests.get(f"{ANALYSIS_API_URL}/sessions/{curr_jules_uuid}", headers=headers)
            if res.status_code == 200:
                current_session = res.json()
                yield f"Monitoring sessions... Status of {current_session.get('title')}: {current_session.get('state', 'UNKNOWN')}", all_reports, session_id, curr_jules_uuid

                # Check for PR in outputs
                outputs = current_session.get("outputs", [])
                pr_url = None
                for out in outputs:
                    if "pullRequest" in out:
                        pr_url = out["pullRequest"]["url"]
                        break

                if pr_url:
                    yield f"PR created for {current_session.get('title')}: {pr_url}. Pulling report...", all_reports, session_id, curr_jules_uuid
                    report_content = pull_report_from_pr(pr_url)
                    all_reports += f"\n\n# Report for {current_session.get('title')}\n\n{report_content}"
                    sessions.pop(i)
                    break # Restart loop since we modified the list
            else:
                print(f"Error polling session {curr_jules_uuid}: {res.text}")

        if sessions:
            time.sleep(30) # Poll every 30 seconds

    # Upon completion, automatically trigger HF upload
    add_log("Analysis complete. Triggering HF upload...")
    deploy_to_hf()

    yield "All sessions complete and changes pushed to HF!", all_reports, session_id, last_jules_uuid

def get_reports_in_branch(repo_full_name, branch_name, filter_type=None):
    if not gh or not repo_full_name or not branch_name:
        return []
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Scanning branch {branch_name} for reports (filter: {filter_type})...")
        
        exclude_files = {"analysis_template.md", "readme.md", "contributing.md", "license.md"}
        
        # Method 1: Check user_experience_reports directory
        reports = []

        # Check for merged slides folder first if we are looking for slides
        if filter_type == "slides":
            try:
                repo.get_contents("user_experience_reports/slides", ref=branch_name)
                reports.append("user_experience_reports/slides")
                add_log("Detected 'user_experience_reports/slides' directory. Added as merged presentation option.")
            except:
                pass

        try:
            contents = repo.get_contents("user_experience_reports", ref=branch_name)
            for content_file in contents:
                name = content_file.name
                if name.endswith(".md"):
                    filename = name.lower()
                    if filename in exclude_files: continue
                    
                    # Optional filtering
                    if filter_type == "report" and "slide" in filename: continue
                    if filter_type == "slides" and "report" in filename: continue
                    
                    path = f"user_experience_reports/{name}"
                    reports.append(path)
        except:
            pass
            
        # Method 2: Recursive scan for ALL Markdown files
        add_log("Deep scanning repository for all Markdown files...")
        tree = repo.get_git_tree(branch_name, recursive=True).tree
        for element in tree:
            if element.type == "blob" and element.path.endswith(".md"):
                path = element.path
                filename = os.path.basename(path).lower()
                if filename in exclude_files:
                    continue
                
                # Optional filtering
                if filter_type == "report" and "slide" in filename: continue
                if filter_type == "slides" and "report" in filename: continue

                if path not in reports:
                    reports.append(path)
        
        # Filter out individual slides if they are inside a slides folder
        if filter_type == "slides":
            folders = [r for r in reports if not r.endswith(".md")]
            if folders:
                reports = [r for r in reports if not any(r.startswith(f + "/") for f in folders)]

        # Sort by relevance
        def sort_key(path):
            p_lower = path.lower()
            score = 0
            # Highest priority: specific report.md and slides.md in user_experience_reports
            if filter_type == "report" and p_lower == "user_experience_reports/report.md": score -= 1000
            if filter_type == "slides" and p_lower == "user_experience_reports/slides.md": score -= 1000
            if filter_type == "slides" and p_lower == "user_experience_reports/slides": score -= 2000
            
            # High priority: other files in user_experience_reports
            if "user_experience_reports" in p_lower: score -= 100
            
            # Medium priority: keywords in filename
            filename = os.path.basename(p_lower)
            if "report" in filename: score -= 50
            if "slide" in filename: score -= 30
            if "ux" in filename: score -= 20
            
            return (score, p_lower)

        reports.sort(key=sort_key)
        
        add_log(f"Discovered {len(reports)} entries.")
        return reports
    except Exception as e:
        add_log(f"Error fetching reports in branch {branch_name}: {e}")
        return []

def get_report_content(repo_full_name, branch_name, report_path):
    if not gh:
        return "Error: GitHub client not initialized. Check your token."
    if not repo_full_name or not branch_name or not report_path:
        return "Please select a repository, branch, and report."
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Fetching content from branch '{branch_name}' at path: {report_path}")
        file_content = repo.get_contents(report_path, ref=branch_name)
        return file_content.decoded_content.decode("utf-8")
    except Exception as e:
        msg = str(e)
        if "404" in msg:
            add_log(f"ERROR: File not found: {report_path} in branch {branch_name}")
            return f"Error: File '{report_path}' not found in branch '{branch_name}'. Please verify the path and branch."
        add_log(f"Error fetching {report_path}: {e}")
        return f"Error fetching report: {str(e)}"

def pull_report_from_pr(pr_url):
    if not gh:
        return "Error: GITHUB_TOKEN not set."

    try:
        # Extract repo and PR number from URL
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
        if not match:
            return "Error: Could not parse PR URL."

        repo_full_name = match.group(1)
        pr_number = int(match.group(2))

        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        branch_name = pr.head.ref

        # Fetch the report files
        reports = get_reports_in_branch(repo_full_name, branch_name)
        if not reports:
            # Try legacy name
            try:
                file_content = repo.get_contents("user_experience_reports/report.md", ref=branch_name)
                content = file_content.decoded_content.decode("utf-8")
                processed_prs.add(pr_number)
                return content
            except:
                return "Report not found yet in this branch."
        
        # Get the first report found
        content = get_report_content(repo_full_name, branch_name, reports[0])
        processed_prs.add(pr_number)
        return content

    except Exception as e:
        print(f"Error pulling report: {e}")
        return f"Error pulling report: {str(e)}"

def render_slides(repo_full_name, branch_name, report_path):
    if not gh:
        return "Error: GitHub client not initialized. Check your token."
    if not repo_full_name or not branch_name or not report_path:
        return "Please select a repository, branch, and report."
    
    try:
        repo = gh.get_repo(repo_full_name)
        content = None
        
        # Check if the path is a directory or points to a slide folder
        is_slides_dir = report_path.endswith("/slides") or report_path.endswith("/slides/")

        if is_slides_dir or "user_experience_reports/slides" in report_path:
            slides_folder = report_path if is_slides_dir else "user_experience_reports/slides"
            try:
                folder_contents = repo.get_contents(slides_folder, ref=branch_name)
                if isinstance(folder_contents, list):
                    add_log(f"Merging multi-file slides from {slides_folder} in branch {branch_name}...")
                    slide_files = [c for c in folder_contents if c.name.endswith(".md")]
                    slide_files.sort(key=lambda x: x.name)
                    
                    merged_content = ""
                    for i, sf in enumerate(slide_files):
                        file_data = repo.get_contents(sf.path, ref=branch_name)
                        slide_text = file_data.decoded_content.decode("utf-8")
                        if i > 0:
                            merged_content += "\n\n---\n\n"
                        merged_content += slide_text
                    
                    content = merged_content
                    add_log(f"Successfully merged {len(slide_files)} slides.")
            except Exception as e:
                add_log(f"Failed to fetch slides from folder: {e}")

        if content is None:
            # Fallback to single file logic
            add_log(f"Attempting to fetch single-file slides from branch '{branch_name}' at path: {report_path}")
            try:
                file_content = repo.get_contents(report_path, ref=branch_name)
                content = file_content.decoded_content.decode("utf-8")
            except Exception as e:
                return f"Error fetching slides: {str(e)}"
            
        # Generate a unique ID for this rendering
        render_id = str(uuid.uuid4())[:8]
        work_dir = f"slides_work_{render_id}"
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, "index.md"), "w") as f:
            f.write(content)
        
        # Set output directory in the SLIDES_OUTPUT_ROOT
        site_name = f"site_{render_id}"
        output_dir = os.path.join(SLIDES_OUTPUT_ROOT, site_name)
            
        subprocess.run(["mkslides", "build", work_dir, "--site-dir", output_dir])
        
        # Cleanup work dir
        shutil.rmtree(work_dir)

        if os.path.exists(os.path.join(output_dir, "index.html")):
            # Return IFrame pointing to the static route
            add_log(f"Slides rendered successfully in {site_name}")
            return f'<iframe src="/static_slides/{site_name}/index.html" width="100%" height="800px" frameborder="0" style="min-height: 80vh;"></iframe>'
        else:
            add_log(f"ERROR: mkslides finished but index.html not found.")
            return "Failed to render slides: index.html not found."
            
    except Exception as e:
        print(f"Error rendering slides: {e}")
        return f"Error rendering slides: {str(e)}"

def get_clickmaps_from_repo(repo_full_name, branch_name):
    if not gh or not repo_full_name or not branch_name:
        return []
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Scanning branch {branch_name} for clickmaps...")

        clickmap_sources = ["user_experience_reports/images", "user_experience_reports/clickmaps"]
        clickmaps = []

        for source in clickmap_sources:
            try:
                contents = repo.get_contents(source, ref=branch_name)
                for c in contents:
                    if c.name.endswith(".png"):
                        # Categorize by filename - Extract problem category
                        # Expected format: clickmap_problem_category_id.png
                        raw_name = c.name.replace(".png", "").replace("clickmap_", "")
                    parts = raw_name.split("_")
                    if len(parts) > 1:
                        category = parts[0].title()
                        desc = " ".join(parts[1:]).title()
                        name = f"[{category}] {desc}"
                    else:
                        name = raw_name.replace("_", " ").title()

                    if (c.download_url, name) not in clickmaps:
                        clickmaps.append((c.download_url, name))
            except:
                continue

        # Sort by name to group categories together
        clickmaps.sort(key=lambda x: x[1])
        return clickmaps
    except Exception as e:
        add_log(f"Error fetching clickmaps: {e}")
        return []

def deploy_to_hf():
    hf_token = os.environ.get("HF_TOKEN")
    hf_space_dest = os.environ.get("HF_SPACE_DEST", "harvesthealth/aux_backup")
    if not hf_token:
        return "❌ Error: HF_TOKEN environment variable not set."

    add_log(f"Deploying to HF Space: {hf_space_dest}...")
    try:
        # Use provided token and revision
        cmd = f"hf upload {hf_space_dest} . --repo-type=space --token {hf_token} --revision main"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            add_log("Deployment successful.")
            return "✅ Deployment successful."
        else:
            add_log(f"Deployment failed: {result.stderr}")
            return f"❌ Deployment failed: {result.stderr}"
    except Exception as e:
        add_log(f"Error during deployment: {e}")
        return f"❌ Error: {str(e)}"

def get_solutions_from_repo(repo_full_name, branch_name):
    if not gh or not repo_full_name or not branch_name:
        return []
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Scanning branch {branch_name} for solutions...")
        try:
            contents = repo.get_contents("user_experience_reports/solutions", ref=branch_name)
            solutions = []
            for c in contents:
                if c.name.endswith(".md"):
                    text = c.decoded_content.decode("utf-8")
                    solutions.append({"name": c.name, "content": text, "path": c.path})
            return solutions
        except:
            return []
    except Exception as e:
        add_log(f"Error fetching solutions: {e}")
        return []

def smart_load_report(repo_name, session_id):
    branches = get_repo_branches(repo_name)
    latest_branch = resolve_branch(repo_name, session_id, branches)
    recovered_uuid = find_jules_session_by_id(session_id)
    if not latest_branch:
        return gr.update(choices=branches), None, f"Branch matching '{session_id}' not found.", f"[ERROR] Branch matching '{session_id}' not found.", recovered_uuid

    reports = get_reports_in_branch(repo_name, latest_branch, filter_type="report")
    best_report = None
    if session_id:
        for r in reports:
            if session_id.lower() in r.lower():
                best_report = r
                break

    if not best_report and reports:
        best_report = reports[0]

    content = ""
    if best_report:
        content = get_report_content(repo_name, latest_branch, best_report)

    log = f"[SYSTEM] Pulled latest branches from {repo_name}\n[SYSTEM] Target branch: {latest_branch}\n[SYSTEM] Found {len(branches)} branches.\n[SYSTEM] Auto-loaded: {best_report}"
    return gr.update(choices=branches, value=latest_branch), gr.update(choices=reports, value=best_report), content, log, recovered_uuid

def smart_load_slides(repo_name, session_id):
    branches = get_repo_branches(repo_name)
    latest_branch = resolve_branch(repo_name, session_id, branches)
    recovered_uuid = find_jules_session_by_id(session_id)
    if not latest_branch:
        return gr.update(choices=branches), None, f"[ERROR] Branch matching '{session_id}' not found.", recovered_uuid, [], gr.update(visible=False), 0, ""

    slides_options = get_reports_in_branch(repo_name, latest_branch, filter_type="slides")
    best_slides = None
    # Priority 1: user_experience_reports/slides (folder)
    # Priority 2: slides.md containing session_id
    # Priority 3: first slides option

    for s in slides_options:
        if s == "user_experience_reports/slides":
            best_slides = s
            break

    if not best_slides and session_id:
        for s in slides_options:
            if session_id.lower() in s.lower():
                best_slides = s
                break

    if not best_slides and slides_options:
        best_slides = slides_options[0]

    html = ""
    carousel_visible = gr.update(visible=False)
    counter_text = ""
    idx = 0
    if best_slides:
        html = render_slides(repo_name, latest_branch, best_slides)
        if len(slides_options) > 1:
            carousel_visible = gr.update(visible=True)
            idx = slides_options.index(best_slides)
            counter_text = f"Deck {idx + 1} of {len(slides_options)}: {best_slides}"

    log = f"[SYSTEM] Pulled latest branches from {repo_name}\n[SYSTEM] Target branch: {latest_branch}\n[SYSTEM] Found {len(branches)} branches.\n[SYSTEM] Auto-loaded slides: {best_slides}"
    return gr.update(choices=branches, value=latest_branch), html, log, recovered_uuid, slides_options, carousel_visible, idx, counter_text

def smart_load_thoughts(repo_name, session_id):
    branches = get_repo_branches(repo_name)
    latest_branch = resolve_branch(repo_name, session_id, branches)
    recovered_uuid = find_jules_session_by_id(session_id)
    if not latest_branch:
        return gr.update(choices=branches), None, f"Branch matching '{session_id}' not found.", f"[ERROR] Branch matching '{session_id}' not found.", recovered_uuid

    logs = get_thought_logs_from_repo(repo_name, latest_branch)
    best_log = None
    if session_id:
        for l in logs:
            if session_id.lower() in l.lower():
                best_log = l
                break

    if not best_log and logs:
        best_log = logs[0]

    content = ""
    if best_log:
        content = get_report_content(repo_name, latest_branch, best_log)

    log = f"[SYSTEM] Pulled latest branches from {repo_name}\n[SYSTEM] Target branch: {latest_branch}\n[SYSTEM] Found {len(branches)} branches.\n[SYSTEM] Auto-loaded log: {best_log}"
    return gr.update(choices=branches, value=latest_branch), gr.update(choices=logs, value=best_log), content, log, recovered_uuid

def get_thought_logs_from_repo(repo_full_name, branch_name):
    if not gh or not repo_full_name or not branch_name:
        return []
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Scanning branch {branch_name} for thought logs...")
        try:
            contents = repo.get_contents("user_experience_reports/thought_logs", ref=branch_name)
            logs = []
            for c in contents:
                if c.name.endswith(".md"):
                    logs.append(c.path)
            return logs
        except:
            return []
    except Exception as e:
        add_log(f"Error fetching thought logs: {e}")
        return []

def generate_agents_prompt(selected_solutions_json):
    if not selected_solutions_json:
        return "No solutions selected."
    try:
        selected_solutions = json.loads(selected_solutions_json)
    except:
        return f"Error parsing solutions: {selected_solutions_json}"

    prompt = """# Coding Agent Prompt: Implement UX Solutions

You are an expert Frontend Developer. Your task is to implement the following "Liked" UX solutions into the project.

## Selected Solutions to Implement:
"""
    for sol in selected_solutions:
        prompt += f"\n### {sol['name']}\n{sol['content']}\n"

    prompt += """
## Instructions:
1. Review the existing UI components.
2. Replace or enhance them using the provided code snippets.
3. Ensure the implementation is responsive and adheres to the project's design system.
4. Verify accessibility and performance after implementation.
"""
    return prompt

def generate_full_ui_call(repo, branch, session_id, selected_solutions_json, url, short_session_id=None):
    if not ANALYSIS_API_KEY:
        return "Error: ANALYSIS_API_KEY missing."

    # Try to recover session_id (the jules uuid) if it looks like a branch name or is missing
    target_uuid = session_id
    if not target_uuid or not target_uuid.startswith("sessions/"):
        # Try multiple fallback search terms
        search_terms = [t for t in [session_id, branch, short_session_id] if t]
        for term in search_terms:
            recovered = find_jules_session_by_id(term)
            if recovered:
                target_uuid = recovered
                break

        if not target_uuid or not target_uuid.startswith("sessions/"):
            return f"Error: Could not find a Jules session for ID '{session_id}', branch '{branch}', or Short ID '{short_session_id}'."

    try:
        if not os.path.exists("ui_generation_template.md"):
            return "Error: ui_generation_template.md not found."
        with open("ui_generation_template.md", "r") as f:
            template = f.read()
    except Exception as e:
        return f"Error reading template: {e}"

    prompt = template.replace("{{selected_solutions}}", selected_solutions_json)
    prompt = prompt.replace("{{url}}", url if url else "the analyzed website")
    prompt = prompt.replace("{{analysis_report}}", "See previous activities in this session")
    prompt = prompt.replace("{{report_id}}", session_id[:8])
    prompt = prompt.replace("{{screenshots_dir}}", f"user_experience_reports/screenshots/{session_id[:8]}")

    headers = {
        "X-Goog-Api-Key": ANALYSIS_API_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "prompt": prompt
    }

    add_log(f"Sending UI generation request to session {target_uuid}...")
    # Ensure target_uuid is used in the URL. If it already starts with 'sessions/', don't prepend it.
    url_suffix = target_uuid if target_uuid.startswith("sessions/") else f"sessions/{target_uuid}"
    response = requests.post(f"{ANALYSIS_API_URL}/{url_suffix}:sendMessage", headers=headers, json=data)
    if response.status_code == 200:
        return f"✅ UI generation requested for session {session_id}. Please wait a few minutes and refresh."
    else:
        add_log(f"API Error: {response.text}")
        return f"❌ Error: {response.text}"

def poll_for_generated_ui(repo_full_name, session_id):
    if not gh or not repo_full_name or not session_id:
        return None
    try:
        branches = get_repo_branches(repo_full_name)
        latest = resolve_branch(repo_full_name, session_id, branches)
        if not latest:
            return "Branch not found."

        repo = gh.get_repo(repo_full_name)
        # Try a few variations of the path
        possible_paths = [
            f"user_experience_reports/generated_ui_{session_id[:8]}.html",
            f"user_experience_reports/generated_ui_{session_id}.html"
        ]

        for path in possible_paths:
            try:
                file_content = repo.get_contents(path, ref=latest)
                return f'<iframe src="{file_content.download_url}" width="100%" height="800px" frameborder="0"></iframe>'
            except:
                continue

        return "UI not generated yet. Please wait..."
    except Exception as e:
        return f"Error polling for UI: {e}"

def blablador_chat_adaptation(message="", history=None, jules_uuid="", session_id=""):
    if history is None:
        history = []

    target_uuid = jules_uuid
    if not target_uuid or not target_uuid.startswith("sessions/"):
        recovered = find_jules_session_by_id(session_id)
        if recovered:
            target_uuid = recovered

    print(f"DEBUG: blablador_chat_adaptation called with message='{message}', history='{history}', target_uuid='{target_uuid}'")
    if not BLABLADOR_API_KEY or not target_uuid:
        history.append(("System", "Error: BLABLADOR_API_KEY or target Jules UUID missing."))
        return history, ""

    # This should call sendMessage to the same target_uuid for real-time adaptation
    # but also use alias-code for the chat experience if desired.
    # The user asked to call alias-code model on blablador endpoint.

    client = get_blablador_client()
    prompt = f"User request for UI adaptation: {message}\n\nPlease update the generated UI and save it."

    try:
        response = client.chat.completions.create(
            model="alias-code",
            messages=[{"role": "user", "content": prompt}]
        )
        agent_msg = response.choices[0].message.content

        # Also notify Jules session to actually do the work if needed
        headers = {"X-Goog-Api-Key": ANALYSIS_API_KEY, "Content-Type": "application/json"}
        url_suffix = target_uuid if target_uuid.startswith("sessions/") else f"sessions/{target_uuid}"
        requests.post(f"{ANALYSIS_API_URL}/{url_suffix}:sendMessage", headers=headers, json={"prompt": message})

        history.append((message, agent_msg))
        return history, ""
    except Exception as e:
        history.append((message, f"Error: {str(e)}"))
        return history, ""

def monitor_repo_for_reports():
    global all_discovered_reports
    if not gh:
        return all_discovered_reports

    add_log("Monitoring repository for new reports across branches...")
    try:
        branches = get_repo_branches(REPO_NAME)
        repo = gh.get_repo(REPO_NAME)

        new_content_found = False
        for branch_name in branches[:25]: # Check top 25 recent branches
            reports = get_reports_in_branch(REPO_NAME, branch_name, filter_type="report")
            
            for report_file in reports:
                report_key = f"{branch_name}/{report_file}"
                if report_key not in processed_prs:
                    try:
                        content = get_report_content(REPO_NAME, branch_name, report_file)
                        report_header = f"\n\n## Discovered Report: {report_file} (Branch: {branch_name})\n\n"
                        all_discovered_reports = report_header + content + "\n\n---\n\n" + all_discovered_reports
                        processed_prs.add(report_key)
                        new_content_found = True
                        add_log(f"New report found: {report_file} in {branch_name}")
                    except:
                        continue
        
        if not new_content_found:
            add_log("No new reports found in recent branches.")

        return all_discovered_reports
    except Exception as e:
        add_log(f"Error monitoring repo: {e}")
        return all_discovered_reports

# Gradio UI
with gr.Blocks(title="UX Analysis Orchestrator") as demo:
    gr.Markdown("# UX Analysis Orchestrator")

    active_session_state = gr.State("")
    active_jules_uuid_state = gr.State("")
    last_generated_tasks_state = gr.State([])
    session_id_sync_list = []
    all_solutions_state = gr.State([])
    selected_solutions_json_state = gr.State("[]")

    jules_uuid_ui = gr.Textbox(label="System UUID", placeholder="Automatically filled after analysis...", render=False)

    with gr.Tabs():
        with gr.Tab("Analysis Orchestrator"):
            gr.Markdown("### Start New Analysis Sessions")
            with gr.Row():
                with gr.Column():
                    theme_input = gr.Textbox(label="Theme", placeholder="e.g., Communication, Purchase decisions, Information gathering")
                    profile_input = gr.Textbox(label="Customer Profile Description", placeholder="Describe the target customer...")
                    num_personas_input = gr.Number(label="Number of Personas", value=1, precision=0)
                    url_input = gr.Textbox(label="Target URL", value="https://example.com")
                    persona_method = gr.Radio(["Example Persona", "TinyTroupe", "DeepPersona"], label="Persona Generation Method", value="TinyTroupe")
                    persona_language = gr.Dropdown(["English", "Spanish", "French", "German", "Chinese", "Japanese", "Russian"], label="Persona Language", value="English")

                    with gr.Column(visible=False) as example_persona_col:
                        gr.Markdown("#### Pre-configured Personas")

                        def update_persona_preview(file):
                            if not file: return ""
                            personas = select_or_create_personas("", "", 1, "Example Persona", file)
                            if personas:
                                p = personas[0]
                                name = p.get('name', 'Unknown')
                                bio = p.get('minibio', '')

                                # Better summary logic
                                summary = f"### Persona: {name}\n"

                                if isinstance(p.get('persona'), dict):
                                    pd = p['persona']
                                    age = pd.get('age', pd.get('persona', {}).get('age', 'N/A'))
                                    occ = pd.get('occupation', {}).get('title', pd.get('persona', {}).get('occupation', {}).get('title', 'N/A'))
                                    summary += f"**Age**: {age} | **Occupation**: {occ}\n\n"

                                summary += f"**Summary**: {bio}"
                                return summary
                            return "Error loading preview."

                        example_personas = get_example_personas()
                        initial_persona = example_personas[0] if example_personas else None
                        example_persona_select = gr.Dropdown(
                            label="Select Example Persona",
                            choices=example_personas,
                            value=initial_persona
                        )
                        example_persona_preview = gr.Markdown(
                            label="Persona Preview",
                            value=update_persona_preview(initial_persona) if initial_persona else ""
                        )

                        example_persona_select.change(fn=update_persona_preview, inputs=[example_persona_select], outputs=[example_persona_preview])

                    def update_method_visibility(method):
                        return gr.update(visible=(method == "Example Persona"))

                    persona_method.change(fn=update_method_visibility, inputs=[persona_method], outputs=[example_persona_col])

                    generate_btn = gr.Button("Generate Personas & Tasks")

                with gr.Column():
                    status_output = gr.Textbox(label="Status", interactive=False)
                    with gr.Row():
                        task_list_display = gr.TextArea(label="Tasks", lines=10, interactive=True, scale=4)
                        with gr.Column(min_width=40, scale=1):
                            save_tasks_btn = gr.Button("✅")
                            cancel_tasks_btn = gr.Button("❌")

                    persona_display = gr.JSON(label="Personas")

                    def save_tasks(tasks_text):
                        tasks = [t.strip() for t in tasks_text.split("\n") if t.strip()]
                        return tasks, "Tasks saved."

                    def cancel_tasks(last_tasks):
                        return "\n".join(last_tasks), "Changes reverted."

                    save_tasks_btn.click(fn=save_tasks, inputs=[task_list_display], outputs=[last_generated_tasks_state, status_output])
                    cancel_tasks_btn.click(fn=cancel_tasks, inputs=[last_generated_tasks_state], outputs=[task_list_display, status_output])

            start_session_btn = gr.Button("Start Analysis Session", variant="primary")
            with gr.Row():
                session_id_orch = gr.Textbox(label="Session ID (GitHub Branch Name)", interactive=True, placeholder="Enter a GitHub branch name to start analysis on...", scale=3)
                download_sid_btn = gr.Button("Download ID", scale=1)

            session_id_download_file = gr.File(label="Session ID Download", visible=False)
            session_id_sync_list.append(session_id_orch)
            report_output = gr.Markdown(label="Active Session Reports")

        with gr.Tab("Presentation Carousel"):
            gr.Markdown("### View Presentation Slides")
            with gr.Row(visible=False):
                sl_repo_select = gr.Dropdown(label="Repository", choices=[REPO_NAME], value=REPO_NAME, interactive=False)
                sl_branch_select = gr.Dropdown(label="Branch", choices=get_repo_branches(REPO_NAME))
            
            with gr.Row():
                session_id_carousel = gr.Textbox(label="Session ID", placeholder="Enter Session ID to pull results...")
                session_id_sync_list.append(session_id_carousel)
                sl_refresh_branches_btn = gr.Button("Pull latest results")

            sl_terminal_log = gr.Code(label="Connection Log", language="shell", value=f"[SYSTEM] Connected to {REPO_NAME}\n[SYSTEM] Ready to pull results.")

            with gr.Row():
                sl_status_display = gr.Markdown("Click 'Pull latest results' to discover slides.")
                sl_render_all_btn = gr.Button("Start Carousel", variant="primary")
                export_gslides_btn = gr.Button("🚀 Export to Google Slides", variant="secondary")

            gslides_status = gr.Markdown(label="Google Slides Status")

            with gr.Row(visible=False) as carousel_controls:
                prev_deck_btn = gr.Button("< Previous Deck")
                deck_counter = gr.Markdown("Deck 0 of 0")
                next_deck_btn = gr.Button("Next Deck >")

            slideshow_display = gr.HTML(label="Slideshow")

            all_decks_state = gr.State([])
            current_deck_idx = gr.State(0)

            def sl_update_branches(repo_name, session_id=None):
                branches = get_repo_branches(repo_name)
                latest = resolve_branch(repo_name, session_id, branches)

                if session_id and not latest:
                    return gr.update(), f"[ERROR] Branch matching '{session_id}' not found. Please wait 30 minutes if newly created."

                log = f"[SYSTEM] Pulled latest branches from {repo_name}\n[SYSTEM] Target branch: {latest}\n[SYSTEM] Found {len(branches)} branches."
                return gr.update(choices=branches, value=latest), log


            sl_repo_select.change(fn=sl_update_branches, inputs=[sl_repo_select], outputs=[sl_branch_select, sl_terminal_log])

            def start_carousel(repo, branch, decks):
                if not decks:
                    return "No slide decks found.", gr.update(visible=False), 0, "No decks."

                # Render first deck
                html = render_slides(repo, branch, decks[0])
                counter_text = f"Deck 1 of {len(decks)}: {decks[0]}"
                return html, gr.update(visible=True), 0, counter_text

            def navigate_carousel(repo, branch, decks, current_idx, direction):
                if not decks: return "", 0, "No decks."
                new_idx = (current_idx + direction) % len(decks)
                html = render_slides(repo, branch, decks[new_idx])
                counter_text = f"Deck {new_idx + 1} of {len(decks)}: {decks[new_idx]}"
                return html, new_idx, counter_text

            sl_refresh_branches_btn.click(fn=smart_load_slides, inputs=[sl_repo_select, session_id_carousel], outputs=[sl_branch_select, slideshow_display, sl_terminal_log, jules_uuid_ui, all_decks_state, carousel_controls, current_deck_idx, deck_counter])

            def sl_smart_auto_render(repo, branch, session_id):
                reports = get_reports_in_branch(repo, branch, filter_type="slides")
                best = None

                # Priority 1: user_experience_reports/slides (folder)
                if "user_experience_reports/slides" in reports:
                    best = "user_experience_reports/slides"

                # Priority 2: contains session_id
                if not best and session_id:
                    for r in reports:
                        if session_id.lower() in r.lower():
                            best = r
                            break

                # Priority 3: first option
                if not best and reports:
                    best = reports[0]

                html = ""
                carousel_visible = gr.update(visible=False)
                status_text = "No slide decks discovered."
                counter_text = ""
                idx = 0

                if best:
                    html = render_slides(repo, branch, best)
                    status_text = f"✅ Auto-loaded slides: `{best}`"
                    if len(reports) > 1:
                        carousel_visible = gr.update(visible=True)
                        idx = reports.index(best)
                        counter_text = f"Deck {idx + 1} of {len(reports)}: {best}"

                return status_text, reports, html, carousel_visible, idx, counter_text

            sl_branch_select.change(
                fn=sl_smart_auto_render,
                inputs=[sl_repo_select, sl_branch_select, session_id_carousel],
                outputs=[sl_status_display, all_decks_state, slideshow_display, carousel_controls, current_deck_idx, deck_counter]
            )

            sl_render_all_btn.click(fn=start_carousel, inputs=[sl_repo_select, sl_branch_select, all_decks_state], outputs=[slideshow_display, carousel_controls, current_deck_idx, deck_counter])

            # Use small helper components for navigation direction
            prev_val = gr.Number(-1, visible=False)
            next_val = gr.Number(1, visible=False)

            prev_deck_btn.click(fn=navigate_carousel, inputs=[sl_repo_select, sl_branch_select, all_decks_state, current_deck_idx, prev_val], outputs=[slideshow_display, current_deck_idx, deck_counter])
            next_deck_btn.click(fn=navigate_carousel, inputs=[sl_repo_select, sl_branch_select, all_decks_state, current_deck_idx, next_val], outputs=[slideshow_display, current_deck_idx, deck_counter])

        with gr.Tab("Report Viewer"):
            gr.Markdown("### View UX Reports & Solutions")
            with gr.Row(visible=False):
                rv_repo_select = gr.Dropdown(label="Repository", choices=[REPO_NAME], value=REPO_NAME, interactive=False)
                rv_branch_select = gr.Dropdown(label="Branch", choices=get_repo_branches(REPO_NAME))

            with gr.Row():
                session_id_rv = gr.Textbox(label="Session ID", placeholder="Enter Session ID to pull results...")
                session_id_sync_list.append(session_id_rv)
                rv_refresh_branches_btn = gr.Button("Pull latest results")

            rv_terminal_log = gr.Code(label="Connection Log", language="shell", value=f"[SYSTEM] Connected to {REPO_NAME}\n[SYSTEM] Ready to pull results.")

            with gr.Row():
                rv_report_select = gr.Dropdown(label="Select Report", choices=[], allow_custom_value=True)
                rv_load_report_btn = gr.Button("Load Report")

            rv_manual_path = gr.Textbox(label="Or enter manual path (e.g. docs/my_report.md)", placeholder="docs/my_report.md")

            with gr.Tabs():
                with gr.Tab("Report"):
                    rv_report_viewer = gr.Markdown(label="Report Content")
                with gr.Tab("Better UI Solutions"):
                    gr.Markdown("Select the solutions you want to include in the full UI generation.")
                    solutions_checkboxes = gr.CheckboxGroup(label="Identified UI Improvements", choices=[])
                    refresh_solutions_btn = gr.Button("Scan for Solutions")
                    solutions_preview = gr.Markdown(label="Solution Previews")

                    def refresh_solutions_ui(repo, branch):
                        sols = get_solutions_from_repo(repo, branch)
                        choices = [s["name"] for s in sols]
                        return gr.update(choices=choices), sols

                    refresh_solutions_btn.click(fn=refresh_solutions_ui, inputs=[rv_repo_select, rv_branch_select], outputs=[solutions_checkboxes, all_solutions_state])

                    def update_selected_solutions(selected_names, all_sols):
                        selected = [s for s in all_sols if s["name"] in selected_names]
                        preview_md = ""
                        for s in selected:
                            preview_md += f"### {s['name']}\n{s['content']}\n\n---\n\n"
                        return json.dumps(selected), preview_md

                    solutions_checkboxes.change(fn=update_selected_solutions, inputs=[solutions_checkboxes, all_solutions_state], outputs=[selected_solutions_json_state, solutions_preview])

            def rv_update_branches(repo_name, session_id=None):
                branches = get_repo_branches(repo_name)
                latest = resolve_branch(repo_name, session_id, branches)

                if session_id and not latest:
                    return gr.update(), f"[ERROR] Branch matching '{session_id}' not found. Please wait 30 minutes if newly created."

                log = f"[SYSTEM] Pulled latest branches from {repo_name}\n[SYSTEM] Target branch: {latest}\n[SYSTEM] Found {len(branches)} branches."
                return gr.update(choices=branches, value=latest), log


            rv_repo_select.change(fn=rv_update_branches, inputs=[rv_repo_select], outputs=[rv_branch_select, rv_terminal_log])
            def rv_load_wrapper(repo, branch, selected, manual):
                path = manual if manual else selected
                return get_report_content(repo, branch, path)

            rv_refresh_branches_btn.click(fn=smart_load_report, inputs=[rv_repo_select, session_id_rv], outputs=[rv_branch_select, rv_report_select, rv_report_viewer, rv_terminal_log, jules_uuid_ui])
            def rv_smart_update_reports(repo, branch, session_id):
                reports = get_reports_in_branch(repo, branch, filter_type="report")
                best = None
                if session_id:
                    for r in reports:
                        if session_id.lower() in r.lower():
                            best = r
                            break
                if not best and reports:
                    best = reports[0]
                return gr.update(choices=reports, value=best)

            rv_branch_select.change(fn=rv_smart_update_reports, inputs=[rv_repo_select, rv_branch_select, session_id_rv], outputs=[rv_report_select])
            rv_load_report_btn.click(fn=rv_load_wrapper, inputs=[rv_repo_select, rv_branch_select, rv_report_select, rv_manual_path], outputs=[rv_report_viewer])

        with gr.Tab("Persona Thought Logs"):
            gr.Markdown("### Persona Internal Monologue & Analysis")
            with gr.Row(visible=False):
                tl_repo_select = gr.Dropdown(label="Repository", choices=[REPO_NAME], value=REPO_NAME, interactive=False)
                tl_branch_select = gr.Dropdown(label="Branch", choices=get_repo_branches(REPO_NAME))

            with gr.Row():
                session_id_tl = gr.Textbox(label="Session ID", placeholder="Enter Session ID to pull results...")
                session_id_sync_list.append(session_id_tl)
                tl_refresh_btn = gr.Button("Pull latest results")

            tl_terminal_log = gr.Code(label="Connection Log", language="shell", value=f"[SYSTEM] Connected to {REPO_NAME}\n[SYSTEM] Ready to pull results.")

            with gr.Row():
                tl_log_select = gr.Dropdown(label="Select Thought Log", choices=[])
                tl_load_btn = gr.Button("Load Log")

            tl_viewer = gr.Markdown(label="Thought Log Content")


            def tl_smart_update_logs(repo, branch, session_id):
                logs = get_thought_logs_from_repo(repo, branch)
                best = None
                if session_id:
                    for l in logs:
                        if session_id.lower() in l.lower():
                            best = l
                            break
                if not best and logs:
                    best = logs[0]
                return gr.update(choices=logs, value=best)

            tl_branch_select.change(fn=tl_smart_update_logs, inputs=[tl_repo_select, tl_branch_select, session_id_tl], outputs=[tl_log_select])

            tl_refresh_btn.click(fn=smart_load_thoughts, inputs=[tl_repo_select, session_id_tl], outputs=[tl_branch_select, tl_log_select, tl_viewer, tl_terminal_log, jules_uuid_ui])
            tl_load_btn.click(fn=get_report_content, inputs=[tl_repo_select, tl_branch_select, tl_log_select], outputs=[tl_viewer])

        with gr.Tab("Average User Journey ClickMaps"):
            gr.Markdown("### ClickMaps")
            with gr.Row():
                session_id_hm = gr.Textbox(label="Session ID", placeholder="Enter Session ID...")
                session_id_sync_list.append(session_id_hm)
            refresh_clickmaps_btn = gr.Button("Refresh ClickMaps")
            clickmap_gallery = gr.Gallery(label="User Interaction ClickMaps", columns=2)

            def smart_load_clickmaps_ui(repo, session_id):
                branches = get_repo_branches(repo)
                latest = resolve_branch(repo, session_id, branches)
                recovered_uuid = find_jules_session_by_id(session_id)
                if not latest:
                    return [], recovered_uuid
                return get_clickmaps_from_repo(repo, latest), recovered_uuid

            refresh_clickmaps_btn.click(fn=smart_load_clickmaps_ui, inputs=[rv_repo_select, session_id_hm], outputs=[clickmap_gallery, jules_uuid_ui])

        with gr.Tab("Agents.txt"):
            gr.Markdown("### Coding Agent Prompt")
            with gr.Row():
                session_id_at = gr.Textbox(label="Session ID", placeholder="Enter Session ID...")
                session_id_sync_list.append(session_id_at)
            refresh_agent_prompt_btn = gr.Button("Generate Prompt for Agent")
            agent_prompt_display = gr.Code(label="Prompt for Coding Agent", language="markdown")

            refresh_agent_prompt_btn.click(fn=generate_agents_prompt, inputs=[selected_solutions_json_state], outputs=[agent_prompt_display])

        with gr.Tab("Full New UI"):
            with gr.Row():
                session_id_ui = gr.Textbox(label="Session ID", placeholder="Enter Session ID (GitHub Branch Name)...")
                session_id_sync_list.append(session_id_ui)
                jules_uuid_ui.render()
            with gr.Row():
                with gr.Column(scale=3):
                    gr.Markdown("### Generated Landing Page")
                    generate_full_ui_btn = gr.Button("Generate Full New UI from Selected Solutions", variant="primary")
                    refresh_ui_btn = gr.Button("Refresh UI Display")
                    full_ui_iframe = gr.HTML(label="Generated UI", value="Click Generate to start.")

                with gr.Column(scale=1):
                    gr.Markdown("### Real-time Adaptation")
                    ui_chatbot = gr.Chatbot(label="Design Chat")
                    ui_chat_msg = gr.Textbox(label="Request Modification", placeholder="e.g. Change primary color to emerald...")
                    ui_chat_send = gr.Button("Send Request")

            generate_full_ui_btn.click(fn=generate_full_ui_call, inputs=[rv_repo_select, rv_branch_select, jules_uuid_ui, selected_solutions_json_state, url_input, session_id_ui], outputs=[full_ui_iframe])
            refresh_ui_btn.click(fn=poll_for_generated_ui, inputs=[rv_repo_select, session_id_ui], outputs=[full_ui_iframe])
            ui_chat_send.click(fn=blablador_chat_adaptation, inputs=[ui_chat_msg, ui_chatbot, jules_uuid_ui, session_id_ui], outputs=[ui_chatbot, ui_chat_msg])


        with gr.Tab("System"):
            gr.Markdown("### System Diagnostics & Manual Connection")
            with gr.Row():
                session_id_sys = gr.Textbox(label="Session ID", placeholder="Enter Session ID...")
                session_id_sync_list.append(session_id_sys)
            with gr.Row():
                sys_token_input = gr.Textbox(label="GitHub Token (Leave blank for default)", type="password")
                sys_repo_input = gr.Textbox(label="Repository (e.g., JsonLord/tiny_web)", value=REPO_NAME, interactive=False)
                sys_test_btn = gr.Button("Test Connection & Fetch Branches")
            
            sys_status = gr.Textbox(label="Connection Status", interactive=False)
            sys_branch_output = gr.JSON(label="Connection Log")

            def system_test(token, repo_name):
                global gh, GITHUB_TOKEN
                try:
                    if token:
                        add_log(f"Testing connection with provided token...")
                        test_gh = Github(auth=Auth.Token(token))
                    elif gh:
                        add_log(f"Testing connection with existing client...")
                        test_gh = gh
                    else:
                        add_log("ERROR: No token provided and default client is missing.")
                        return "Error: No GitHub client available. Please provide a token.", None
                    
                    user = test_gh.get_user().login
                    add_log(f"Successfully authenticated as {user}")
                    
                    # Update global client if token was provided
                    if token:
                        gh = test_gh
                        GITHUB_TOKEN = token
                        add_log("Global GitHub client updated with new token.")
                    
                    status = f"Success: Connected as {user} to {repo_name}"
                    
                    # Use existing optimized logic
                    branches = get_repo_branches(repo_name, github_client=test_gh)
                    latest_branch = branches[0] if branches else "None"
                    
                    return status, {"status": "Connection established successfully", "user": user, "latest_branch": latest_branch}
                except Exception as e:
                    add_log(f"System Test Error: {str(e)}")
                    return f"Error: {str(e)}", {"status": "Connection failed", "error": str(e)}

            sys_test_btn.click(fn=system_test, inputs=[sys_token_input, sys_repo_input], outputs=[sys_status, sys_branch_output])

        with gr.Tab("Live Monitoring"):
            gr.Markdown("### Live Monitoring of JsonLord/tiny_web for new UX reports")
            with gr.Row():
                session_id_live = gr.Textbox(label="Session ID", placeholder="Enter Session ID...")
                session_id_sync_list.append(session_id_live)
            live_log = gr.Textbox(label="GitHub Connection Logs", lines=5, interactive=False)
            refresh_feed_btn = gr.Button("Refresh Feed Now")
            global_feed = gr.Markdown(value="Waiting for new reports...")
            
            def monitor_and_log():
                reports = monitor_repo_for_reports()
                logs = "\n".join(github_logs[-20:])
                return reports, logs

            # Use a Timer to poll every 60 seconds
            timer = gr.Timer(value=60)
            timer.tick(fn=monitor_and_log, outputs=[global_feed, live_log])
            refresh_feed_btn.click(fn=monitor_and_log, outputs=[global_feed, live_log])


        with gr.Tab("Alternative Styling"):
            gr.Markdown("### Design Automation & Iteration")
            gr.Markdown("We are working with the team behind https://github.com/onlook-dev/onlook to automate fast design iterations based on the user test reports. Stay updated on changes to the Github Page by following it.")

            gr.Markdown("---")
            gr.Markdown("### 🚀 Recommendations for Customer-Facing Application")
            gr.Markdown("""
            To transform this prototype into a production-ready customer application, we recommend the following enhancements:

            1. **Multi-Tenant Authentication**: Implement Clerk or NextAuth for secure user logins and project isolation, ensuring customers only see their own analysis branches.
            2. **Real-Time Step Visualization**: Replace the static status logs with a real-time progress bar and a "Live View" tab showing Jules' browser interactions as they happen.
            3. **Figma/Design Integration**: Develop a plugin to export the "Identified UI Improvements" directly into Figma as annotated design layers.
            4. **Guided Onboarding Flow**: Add a "Wizard" mode for first-time users to help them define their Theme and Customer Profile through guided questions.
            5. **Result Comparison (A/B Testing)**: Add a feature to view the original landing page side-by-side with the Generated UI, including a "Scorecard" of UX metrics (Accessibility, Conversion, Clarity).
            6. **Automated Deployment Previews**: Integrate with Vercel/Netlify APIs to automatically deploy the 'Full New UI' to a shareable preview URL upon generation.
            """)

            gr.Markdown("---")
            gr.Markdown("### 🛠️ Manual Deployment")
            manual_deploy_btn = gr.Button("Push App Changes to Hugging Face Space")
            deploy_status = gr.Markdown()
            manual_deploy_btn.click(fn=deploy_to_hf, outputs=[deploy_status])

    # Persona Preview Handler (moved to a safe place if not already there)
    # Actually it's inside the Tab block in previous edit.

    # Event handlers
    export_gslides_btn.click(
        fn=export_to_google_slides,
        inputs=[session_id_carousel],
        outputs=[gslides_status]
    )

    download_sid_btn.click(
        fn=download_session_id_file,
        inputs=[session_id_orch],
        outputs=[session_id_download_file]
    ).then(
        fn=lambda: gr.update(visible=True),
        outputs=[session_id_download_file]
    )

    generate_btn.click(
        fn=handle_generate,
        inputs=[theme_input, profile_input, num_personas_input, persona_method, example_persona_select, url_input, persona_language],
        outputs=[status_output, task_list_display, persona_display, last_generated_tasks_state]
    )

    start_session_btn.click(
        fn=start_and_monitor_sessions,
        inputs=[persona_display, last_generated_tasks_state, url_input, session_id_orch, persona_language],
        outputs=[status_output, report_output, session_id_orch, active_jules_uuid_state]
    ).then(
        fn=lambda x: [x] * len(session_id_sync_list),
        inputs=[session_id_orch],
        outputs=session_id_sync_list
    ).then(
        fn=lambda x: x,
        inputs=[active_jules_uuid_state],
        outputs=[jules_uuid_ui]
    )

    # Session ID Sync
    def sync_session_ids(val):
        return [val] * len(session_id_sync_list)

    for sid in session_id_sync_list:
        if sid.interactive:
            sid.change(fn=sync_session_ids, inputs=[sid], outputs=session_id_sync_list)
            sid.change(fn=lambda x: x, inputs=[sid], outputs=[active_session_state])

if __name__ == "__main__":
    # Startup connectivity check
    print("--- STARTUP GITHUB CONNECTIVITY CHECK ---")
    token_source = "None"
    if os.environ.get("GITHUB_TOKEN"):
        token_source = "GITHUB_TOKEN"
    elif os.environ.get("GITHUB_API_TOKEN"):
        token_source = "GITHUB_API_TOKEN"
    
    print(f"Token Source: {token_source}")

    if gh is None:
        print(f"ERROR: No GitHub token found in GITHUB_TOKEN or GITHUB_API_TOKEN.")
    else:
        try:
            user = gh.get_user().login
            print(f"SUCCESS: Logged in to GitHub as: {user}")
            
            # Test branch fetching for REPO_NAME
            print(f"Testing branch fetch for {REPO_NAME}...")
            test_branches = get_repo_branches(REPO_NAME)
            print(f"Test branch fetch successful. Found {len(test_branches)} branches.")
        except Exception as startup_err:
            print(f"ERROR: GitHub connectivity test failed: {startup_err}")
    print("-----------------------------------------")

    # Wrap with FastAPI for health check and API endpoints
    fastapi_app = FastAPI()

    @fastapi_app.get("/health")
    def health():
        return {"status": "ok"}

    @fastapi_app.get("/api/info")
    def info():
        return {"app": "UX Analysis Orchestrator", "version": "1.0.0"}

    # Mount static files for slides
    fastapi_app.mount("/static_slides", StaticFiles(directory=SLIDES_OUTPUT_ROOT), name="static_slides")

    # Mount Gradio
    # Restrict allowed_paths for better security
    demo_app = gr.mount_gradio_app(fastapi_app, demo, path="/", allowed_paths=["/app"])

    # Run uvicorn
    uvicorn.run(demo_app, host="0.0.0.0", port=7860)
