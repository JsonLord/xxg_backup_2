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

# Logic to clone TinyTroupe on startup if not present
def clone_tinytroupe():
    if not os.path.exists("external/TinyTroupe"):
        print("Cloning TinyTroupe...")
        os.makedirs("external", exist_ok=True)
        subprocess.run([
            "git", "clone", "-b", "fix/jules-final-submission-branch",
            "https://github.com/JsonLord/TinyTroupe.git", "external/TinyTroupe"
        ])
        patch_tinytroupe()
    else:
        print("TinyTroupe already present.")
        patch_tinytroupe()

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

        # 2. Ensure alias-large is used (revert any previous fast patches)
        content = content.replace('"alias-fast"', '"alias-large"')

        # 3. Handle 502 errors by waiting 35 seconds and setting a parallel retry flag
        # We need to modify the send_message loop

        # Inject parallel_retry = False before the loop
        content = content.replace("i = 0", "parallel_retry = False\n        i = 0")

        # Modify the model call inside the loop
        old_call = "response = self._raw_model_call(model, chat_api_params)"
        new_call = """if parallel_retry:
                        logger.info("Attempting parallel call to alias-large and alias-huge.")
                        response = self._raw_model_call_parallel(["alias-large", "alias-huge"], chat_api_params)
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

clone_tinytroupe()

def setup_mkslides():
    if not os.path.exists("external/mkslides"):
        print("Cloning mkslides...")
        os.makedirs("external", exist_ok=True)
        subprocess.run([
            "git", "clone", "--recursive",
            "https://github.com/MartenBE/mkslides.git", "external/mkslides"
        ])
        # Patch pyproject.toml to allow Python 3.12
        pyproject_path = "external/mkslides/pyproject.toml"
        if os.path.exists(pyproject_path):
            with open(pyproject_path, "r") as f:
                content = f.read()
            content = content.replace('requires-python = ">=3.13"', 'requires-python = ">=3.12"')
            with open(pyproject_path, "w") as f:
                f.write(content)
        
        # Install dependencies and mkslides
        subprocess.run(["pip", "install", "./external/mkslides"])
    else:
        print("mkslides directory already present. Ensuring it is installed...")
        subprocess.run(["pip", "install", "./external/mkslides"])

    # Verify installation
    res = subprocess.run(["which", "mkslides"], capture_output=True, text=True)
    if res.returncode == 0:
        print(f"mkslides verified at: {res.stdout.strip()}")
    else:
        print("WARNING: mkslides CLI not found in PATH after installation.")

setup_mkslides()

import gradio as gr
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_TOKEN")
JULES_API_KEY = os.environ.get("JULES_API_KEY")
BLABLADOR_API_KEY = os.environ.get("BLABLADOR_API_KEY")
BLABLADOR_BASE_URL = "https://api.helmholtz-blablador.fz-juelich.de/v1"
JULES_API_URL = "https://jules.googleapis.com/v1alpha"

# GitHub Client
gh = Github(auth=Auth.Token(GITHUB_TOKEN)) if GITHUB_TOKEN else None
REPO_NAME = "JsonLord/tiny_web"
POOL_REPO_NAME = "JsonLord/agent-notes"
POOL_PATH = "PersonaPool"

# Global state for processed reports
processed_prs = set()
all_discovered_reports = ""
github_logs = []

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

def select_or_create_personas(theme, customer_profile, num_personas):
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
            model="alias-large",
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

def generate_persona_from_endpoint(theme, customer_profile):
    add_log("Attempting persona generation from THzva/deeppersona-experience...")
    client = get_blablador_client()
    if not client:
        return None

    # Step 1: Breakdown profile into parameters using LLM
    prompt = f"""
    Break down the following theme and customer profile into the specific attributes required for a detailed persona.
    Theme: {theme}
    Profile: {customer_profile}

    Return a JSON object with the following fields:
    - name (string, full name)
    - age (int)
    - gender (string)
    - occupation (string)
    - city (string)
    - country (string)
    - custom_values (string, comma separated)
    - custom_life_attitude (string)
    - life_story (string, a brief background)
    - interests_hobbies (string, comma separated)

    CRITICAL: Return ONLY the JSON object.
    """

    try:
        response = client.chat.completions.create(
            model="alias-large",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        params = json.loads(response.choices[0].message.content)
        add_log(f"Profile breakdown complete for {params.get('occupation')}")

        # Step 2: Call the new generation endpoint
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

        # Step 3: Use the name from params or result
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
        add_log(f"Endpoint generation failed: {e}")
        return None

def generate_personas(theme, customer_profile, num_personas):
    add_log(f"Generating {num_personas} personas...")
    
    # Try the new endpoint first
    final_personas = []
    for i in range(int(num_personas)):
        p = generate_persona_from_endpoint(theme, customer_profile)
        if p:
            final_personas.append(p)
    
    if len(final_personas) == int(num_personas):
        add_log("Successfully generated all personas from endpoint.")
        return final_personas
    
    add_log("Falling back to TinyTroupe logic for remaining personas...")
    
    # Ensure alias-large is used
    config_manager.update("model", "alias-large")
    config_manager.update("reasoning_model", "alias-large")

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

def generate_tasks(theme, customer_profile):
    client = get_blablador_client()
    if not client:
        return [f"Task {i+1} for {theme} (BLABLADOR_API_KEY not set)" for i in range(10)]

    prompt = f"""
    Generate 10 sequential tasks for a user to perform on a website related to the theme: {theme}.
    The user profile is: {customer_profile}.

    The tasks should cover:
    1. Communication
    2. Purchase decisions
    3. Custom Search / Information gathering
    4. Emotional connection to the persona and content/styling

    The tasks must be in sequential order.

    CRITICAL: You MUST return a JSON object with a "tasks" key containing a list of strings.
    Example: {{"tasks": ["task 1", "task 2", ...]}}
    Do not include any other text in your response.
    """

    models_to_try = ["alias-large", "alias-huge", "alias-fast"]

    for attempt in range(5):
        try:
            print(f"Attempt {attempt+1} for task generation...")
            if attempt > 0:
                print(f"Retrying in parallel with {models_to_try}")
                # Wait 35s if it's a retry (likely Proxy Error or Rate Limit)
                time.sleep(35)
                response = call_llm_parallel(client, models_to_try, [{"role": "user", "content": prompt}])
            else:
                response = client.chat.completions.create(
                    model="alias-large",
                    messages=[{"role": "user", "content": prompt}]
                )

            if response and not isinstance(response, Exception):
                content = response.choices[0].message.content
                # Robust extraction
                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    try:
                        tasks_json = json.loads(json_match.group())
                        tasks = tasks_json.get("tasks", [])
                        if tasks and isinstance(tasks, list) and len(tasks) >= 5:
                            return tasks[:10]
                    except:
                        pass

                # Fallback: try to extract lines that look like tasks
                lines = [re.sub(r'^\d+[\.\)]\s*', '', l).strip() for l in content.split('\n') if l.strip()]
                tasks = [l for l in lines if len(l) > 20 and not l.startswith('{') and not l.startswith('`')]
                if len(tasks) >= 5:
                    return tasks[:10]

            print(f"Attempt {attempt+1} failed to yield valid tasks.")
        except Exception as e:
            print(f"Error in attempt {attempt+1}: {e}")

    return [f"Task {i+1} for {theme} (Manual fallback)" for i in range(10)]

def handle_generate(theme, customer_profile, num_personas):
    try:
        yield "Generating tasks...", None, None
        tasks = generate_tasks(theme, customer_profile)

        yield "Selecting or creating personas...", tasks, None
        personas = select_or_create_personas(theme, customer_profile, num_personas)

        yield "Generation complete!", tasks, personas
    except Exception as e:
        yield f"Error during generation: {str(e)}", None, None

def start_and_monitor_sessions(personas, tasks, url):
    repo_name = "JsonLord/tiny_web"
    branch_name = "main"

    if not JULES_API_KEY:
        yield "Error: JULES_API_KEY not set.", ""
        return

    with open("jules_template.md", "r") as f:
        template = f.read()

    sessions = []
    for persona in personas:
        # Generate unique report ID
        report_id = str(uuid.uuid4())[:8]
        
        # Format prompt
        prompt = template.replace("{{persona_context}}", json.dumps(persona))
        prompt = prompt.replace("{{tasks_list}}", json.dumps(tasks))
        prompt = prompt.replace("{{url}}", url)
        prompt = prompt.replace("{{report_id}}", report_id)
        prompt = prompt.replace("{{blablador_api_key}}", BLABLADOR_API_KEY if BLABLADOR_API_KEY else "YOUR_API_KEY")

        # Call Jules API
        headers = {
            "X-Goog-Api-Key": JULES_API_KEY,
            "Content-Type": "application/json"
        }
        data = {
            "prompt": prompt,
            "sourceContext": {
                "source": f"sources/github/{repo_name}",
                "githubRepoContext": {
                    "startingBranch": branch_name
                }
            },
            "automationMode": "AUTO_CREATE_PR",
            "title": f"UX Analysis for {persona['name']}"
        }

        response = requests.post(f"{JULES_API_URL}/sessions", headers=headers, json=data)
        if response.status_code == 200:
            sessions.append(response.json())
        else:
            yield f"Error creating session for {persona['name']}: {response.text}", ""
            return

    # Monitoring
    all_reports = ""
    while sessions:
        for i, session in enumerate(sessions):
            session_id = session['id']
            res = requests.get(f"{JULES_API_URL}/sessions/{session_id}", headers=headers)
            if res.status_code == 200:
                current_session = res.json()
                yield f"Monitoring sessions... Status of {current_session.get('title')}: {current_session.get('state', 'UNKNOWN')}", all_reports

                # Check for PR in outputs
                outputs = current_session.get("outputs", [])
                pr_url = None
                for out in outputs:
                    if "pullRequest" in out:
                        pr_url = out["pullRequest"]["url"]
                        break

                if pr_url:
                    yield f"PR created for {current_session.get('title')}: {pr_url}. Pulling report...", all_reports
                    report_content = pull_report_from_pr(pr_url)
                    all_reports += f"\n\n# Report for {current_session.get('title')}\n\n{report_content}"
                    sessions.pop(i)
                    break # Restart loop since we modified the list
            else:
                print(f"Error polling session {session_id}: {res.text}")

        if sessions:
            time.sleep(30) # Poll every 30 seconds

    yield "All sessions complete!", all_reports

def get_reports_in_branch(repo_full_name, branch_name, filter_type=None):
    if not gh or not repo_full_name or not branch_name:
        return []
    try:
        repo = gh.get_repo(repo_full_name)
        add_log(f"Scanning branch {branch_name} for reports (filter: {filter_type})...")
        
        exclude_files = {"jules_template.md", "readme.md", "contributing.md", "license.md"}
        
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
        
        add_log(f"Discovered {len(reports)} potential Markdown files.")
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
        
        # Method 1: Check for multi-file slides folder
        # We check this first if the report_path is in user_experience_reports or if it's default
        if "user_experience_reports" in report_path:
            # If the user selected the folder itself or a file within a folder that has a slides subfolder
            if report_path.endswith("/slides") or report_path.endswith("/slides/"):
                slides_folder = report_path
            else:
                slides_folder = "user_experience_reports/slides"

            try:
                folder_contents = repo.get_contents(slides_folder, ref=branch_name)
                if isinstance(folder_contents, list):
                    add_log(f"Multi-file slides folder found in branch {branch_name}. Merging...")
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
            except:
                pass

        if content is None:
            # Method 2: Single file logic (legacy/fallback)
            # Determine slides path
            if "slide" in report_path.lower():
                slides_path = report_path
            elif report_path == "user_experience_reports/report.md":
                slides_path = "user_experience_reports/slides.md"
            else:
                # Try to map report_ID.md to slides_ID.md
                slides_path = report_path.replace("report_", "slides_")
                if slides_path == report_path: # No replacement happened
                     slides_path = "user_experience_reports/slides.md" # fallback

            add_log(f"Attempting to fetch single-file slides from branch '{branch_name}' at path: {slides_path}")

            try:
                file_content = repo.get_contents(slides_path, ref=branch_name)
                content = file_content.decoded_content.decode("utf-8")
            except Exception as e:
                if "404" in str(e):
                    add_log(f"Slides file not found at {slides_path}. Attempting fallback...")
                    # Last resort fallback: look for any .md file with 'slides' in the name in the same branch
                    reports = get_reports_in_branch(repo_full_name, branch_name)
                    slides_files = [r for r in reports if "slide" in r.lower() and "/slides/" not in r]
                    if slides_files:
                        slides_path = slides_files[0]
                        add_log(f"Found alternative slides file: {slides_path}")
                        file_content = repo.get_contents(slides_path, ref=branch_name)
                        content = file_content.decoded_content.decode("utf-8")
                    else:
                        return f"Error: File '{slides_path}' not found in branch '{branch_name}'. No other slide files discovered."
                else:
                    add_log(f"Error fetching slides: {e}")
                    return f"Error fetching slides: {str(e)}"
            
        # Prepare workspace
        report_id = str(uuid.uuid4())[:8]
        work_dir = f"slides_work_{report_id}"
        os.makedirs(work_dir, exist_ok=True)
        with open(f"{work_dir}/index.md", "w") as f:
            f.write(content)
        
        # Run mkslides
        output_dir = f"slides_site_{report_id}"
        # Ensure we have a clean output dir
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
            
        subprocess.run(["mkslides", "build", work_dir, "--site-dir", output_dir])
        
        if os.path.exists(f"{output_dir}/index.html"):
            # Return IFrame pointing to the generated site. 
            # Use a path relative to the current working directory for Gradio's /file endpoint
            rel_path = os.path.relpath(f"{output_dir}/index.html", os.getcwd())
            add_log(f"Slides rendered. Serving from relative path: {rel_path}")

            # Use 'file={rel_path}' which is more robust in HF Spaces than '/file={abs_path}'
            return f'<iframe src="file={rel_path}" width="100%" height="600px" frameborder="0"></iframe>'
        else:
            return "Failed to render slides."
            
    except Exception as e:
        print(f"Error rendering slides: {e}")
        return f"Error rendering slides: {str(e)}"

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
with gr.Blocks() as demo:
    gr.Markdown("# Jules UX Analysis Orchestrator")

    with gr.Tabs():
        with gr.Tab("Orchestrator"):
            gr.Markdown("### Start New Jules Sessions")
            with gr.Row():
                with gr.Column():
                    theme_input = gr.Textbox(label="Theme", placeholder="e.g., Communication, Purchase decisions, Information gathering")
                    profile_input = gr.Textbox(label="Customer Profile Description", placeholder="Describe the target customer...")
                    num_personas_input = gr.Number(label="Number of Personas", value=1, precision=0)
                    url_input = gr.Textbox(label="Target URL", value="https://example.com")
                    generate_btn = gr.Button("Generate Personas & Tasks")

                with gr.Column():
                    status_output = gr.Textbox(label="Status", interactive=False)
                    task_list_display = gr.JSON(label="Tasks")
                    persona_display = gr.JSON(label="Personas")

            start_session_btn = gr.Button("Start Jules Session", variant="primary")
            report_output = gr.Markdown(label="Active Session Reports")

        with gr.Tab("Report Viewer"):
            gr.Markdown("### View UX Reports")
            with gr.Row():
                rv_repo_select = gr.Dropdown(label="Repository", choices=get_user_repos(), value=REPO_NAME)
                rv_branch_select = gr.Dropdown(label="Branch", choices=get_repo_branches(REPO_NAME))
                rv_refresh_branches_btn = gr.Button("Refresh Branches")
            
            with gr.Row():
                rv_report_select = gr.Dropdown(label="Select Report", choices=[], allow_custom_value=True)
                rv_load_report_btn = gr.Button("Load Report")
            
            rv_manual_path = gr.Textbox(label="Or enter manual path (e.g. docs/my_report.md)", placeholder="docs/my_report.md")

            rv_report_viewer = gr.Markdown(label="Report Content")
            
            def rv_update_branches(repo_name):
                branches = get_repo_branches(repo_name)
                latest = branches[0] if branches else "main"
                return gr.update(choices=branches, value=latest)

            def rv_update_reports(repo_name, branch_name):
                reports = get_reports_in_branch(repo_name, branch_name, filter_type="report")
                return gr.update(choices=reports, value=reports[0] if reports else None)

            rv_repo_select.change(fn=rv_update_branches, inputs=[rv_repo_select], outputs=[rv_branch_select])
            def rv_load_wrapper(repo, branch, selected, manual):
                path = manual if manual else selected
                return get_report_content(repo, branch, path)

            rv_refresh_branches_btn.click(fn=rv_update_branches, inputs=[rv_repo_select], outputs=[rv_branch_select])
            rv_branch_select.change(fn=rv_update_reports, inputs=[rv_repo_select, rv_branch_select], outputs=[rv_report_select])
            rv_load_report_btn.click(fn=rv_load_wrapper, inputs=[rv_repo_select, rv_branch_select, rv_report_select, rv_manual_path], outputs=[rv_report_viewer])

        with gr.Tab("Slideshow"):
            gr.Markdown("### View Presentation Slides")
            with gr.Row():
                sl_repo_select = gr.Dropdown(label="Repository", choices=get_user_repos(), value=REPO_NAME)
                sl_branch_select = gr.Dropdown(label="Branch", choices=get_repo_branches(REPO_NAME))
                sl_refresh_branches_btn = gr.Button("Refresh Branches")
            
            with gr.Row():
                sl_report_select = gr.Dropdown(label="Select Report/Slides File", choices=[], allow_custom_value=True)
                sl_render_btn = gr.Button("Render Slideshow")
            
            sl_manual_path = gr.Textbox(label="Or enter manual path (e.g. docs/slides.md)", placeholder="docs/slides.md")

            slideshow_display = gr.HTML(label="Slideshow")

            def sl_update_branches(repo_name):
                branches = get_repo_branches(repo_name)
                latest = branches[0] if branches else "main"
                return gr.update(choices=branches, value=latest)

            def sl_update_reports(repo_name, branch_name):
                reports = get_reports_in_branch(repo_name, branch_name, filter_type="slides")
                return gr.update(choices=reports, value=reports[0] if reports else None)

            sl_repo_select.change(fn=sl_update_branches, inputs=[sl_repo_select], outputs=[sl_branch_select])
            def sl_render_wrapper(repo, branch, selected, manual):
                path = manual if manual else selected
                return render_slides(repo, branch, path)

            sl_refresh_branches_btn.click(fn=sl_update_branches, inputs=[sl_repo_select], outputs=[sl_branch_select])
            sl_branch_select.change(fn=sl_update_reports, inputs=[sl_repo_select, sl_branch_select], outputs=[sl_report_select])
            sl_render_btn.click(fn=sl_render_wrapper, inputs=[sl_repo_select, sl_branch_select, sl_report_select, sl_manual_path], outputs=[slideshow_display])

        with gr.Tab("System"):
            gr.Markdown("### System Diagnostics & Manual Connection")
            with gr.Row():
                sys_token_input = gr.Textbox(label="GitHub Token (Leave blank for default)", type="password")
                sys_repo_input = gr.Textbox(label="Repository (e.g., JsonLord/tiny_web)", value=REPO_NAME)
                sys_test_btn = gr.Button("Test Connection & Fetch Branches")
            
            sys_status = gr.Textbox(label="Connection Status", interactive=False)
            sys_branch_output = gr.JSON(label="Discovered Branches")

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
                    
                    return status, branches
                except Exception as e:
                    add_log(f"System Test Error: {str(e)}")
                    return f"Error: {str(e)}", None

            sys_test_btn.click(fn=system_test, inputs=[sys_token_input, sys_repo_input], outputs=[sys_status, sys_branch_output])

        with gr.Tab("Live Monitoring"):
            gr.Markdown("### Live Monitoring of JsonLord/tiny_web for new UX reports")
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

    # Event handlers
    generate_btn.click(
        fn=handle_generate,
        inputs=[theme_input, profile_input, num_personas_input],
        outputs=[status_output, task_list_display, persona_display]
    )

    start_session_btn.click(
        fn=start_and_monitor_sessions,
        inputs=[persona_display, task_list_display, url_input],
        outputs=[status_output, report_output]
    )

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

    # Allow current directory for file serving, specifically for slides_site_*
    demo.launch(allowed_paths=[os.getcwd()])
