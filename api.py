from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from knowledge_storm import STORMWikiRunnerArguments, STORMWikiRunner, STORMWikiLMConfigs
from knowledge_storm.lm import OpenAIModel
import os
import tempfile
import json

app = FastAPI()

class GenerateRequest(BaseModel):
    topic: str
    do_polish_article: bool = True

class CitationRequest(BaseModel):
    article_text: str
    search_top_k: int = 5
    topic: str = None

def setup_topic_directory(temp_dir: str, topic: str):
    """Setup directory structure for a topic"""
    topic_dir = os.path.join(temp_dir, topic.replace(" ", "_"))
    os.makedirs(topic_dir, exist_ok=True)
    
    # Create initial conversation log in topic directory
    conv_log_path = os.path.join(topic_dir, "conversation_log.json")
    if not os.path.exists(conv_log_path):
        with open(conv_log_path, "w") as f:
            json.dump([], f)
    
    return topic_dir

def get_storm_runner(with_retrieval=False, search_top_k=3, topic=None):
    lm_configs = STORMWikiLMConfigs()
    openai_kwargs = {
        'api_key': os.getenv("OPENAI_API_KEY"),
        'temperature': 1.0,
        'top_p': 0.9,
    }

    gpt_35_model_name = 'gpt-3.5-turbo'
    gpt_4_model_name = 'gpt-4'
    
    # Set up language models
    conv_simulator_lm = OpenAIModel(model=gpt_35_model_name, max_tokens=500, **openai_kwargs)
    question_asker_lm = OpenAIModel(model=gpt_35_model_name, max_tokens=500, **openai_kwargs)
    outline_gen_lm = OpenAIModel(model=gpt_4_model_name, max_tokens=400, **openai_kwargs)
    article_gen_lm = OpenAIModel(model=gpt_4_model_name, max_tokens=700, **openai_kwargs)
    article_polish_lm = OpenAIModel(model=gpt_4_model_name, max_tokens=4000, **openai_kwargs)

    lm_configs.set_conv_simulator_lm(conv_simulator_lm)
    lm_configs.set_question_asker_lm(question_asker_lm)
    lm_configs.set_outline_gen_lm(outline_gen_lm)
    lm_configs.set_article_gen_lm(article_gen_lm)
    lm_configs.set_article_polish_lm(article_polish_lm)

    # Create temporary directory for STORM outputs
    temp_dir = tempfile.mkdtemp()
    
    # Create topic directory if provided
    if topic:
        setup_topic_directory(temp_dir, topic)
    
    # Initialize retrieval module if needed
    rm = None
    if with_retrieval:
        from knowledge_storm.rm import YouRM
        rm = YouRM(ydc_api_key=os.getenv('YDC_API_KEY'), k=search_top_k)

    # Create engine arguments
    engine_args = STORMWikiRunnerArguments(
        output_dir=temp_dir,
        max_conv_turn=3,
        max_perspective=3,
        search_top_k=search_top_k if with_retrieval else 0,
        max_thread_num=3,
    )

    return STORMWikiRunner(engine_args, lm_configs, rm=rm), temp_dir

def extract_topic(article_text: str, lm_config: STORMWikiLMConfigs) -> str:
    """Extract main topic from article text using LLM."""
    prompt = f"""Extract the main topic or subject from this text in 2-3 words:
    
    {article_text[:1000]}...
    
    Topic:"""
    
    response = lm_config.conv_simulator_lm.complete(prompt)
    return response.strip()

@app.post("/generate")
async def generate_article(request: GenerateRequest):
    try:
        # Initialize STORM without retrieval
        runner, temp_dir = get_storm_runner(with_retrieval=False, topic=request.topic)
        
        # Generate article
        article = runner.run(
            topic=request.topic,
            do_research=True,
            do_generate_outline=True,
            do_generate_article=True,
            do_polish_article=request.do_polish_article
        )
        
        return {
            "topic": request.topic,
            "article": article
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/find-citations")
async def find_citations(request: CitationRequest):
    try:
        # Create temporary runner to extract topic if needed
        if not request.topic:
            temp_runner, _ = get_storm_runner()
            topic = extract_topic(request.article_text, temp_runner.lm_configs)
        else:
            topic = request.topic
            
        # Initialize STORM with retrieval enabled
        runner, temp_dir = get_storm_runner(
            with_retrieval=True, 
            search_top_k=request.search_top_k,
            topic=topic
        )
        
        # Run research to find citations
        search_results = runner.run_knowledge_curation_module(
            topic=topic,
            article_text=request.article_text
        )
        
        citations = []
        if search_results:
            citations = [
                {
                    "url": result.url,
                    "title": result.title,
                    "snippet": result.snippet
                }
                for result in search_results.results
            ]
        
        return {
            "topic": topic,
            "citations": citations,
            "message": f"Found {len(citations)} citations"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy"} 