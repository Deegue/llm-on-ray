## Serving with Deepspeed
* First you need to install llm-on-ray with deespeed dependencies as well as Intel OneAPI.
```bash
pip install .[cpu,deepspeed] -f https://developer.intel.com/ipex-whl-stable-cpu -f https://download.pytorch.org/whl/torch_stable.html
sudo ./install-oneapi.sh
# optonal
ds_report
```
* Start up ray cluster
```bash
./start-ray-cluster.sh
```
* Deploy serving with Deepspeed and verfiy the inference
```bash
# If you dont' want to view serve logs, you can set env var, "KEEP_SERVE_TERMINAL" to false

# Run model serve with specified model and tokenizer
python inference/run_model_serve.py --model $model --tokenizer $tokenizer --deepspeed

# INFO - Deployment 'custom-model_PredictDeployment' is ready at `http://127.0.0.1:8000/custom-model`. component=serve deployment=custom-model_PredictDeployment
# Service is deployed successfully

# Verfiy the inference on deployed model
python inference/run_model_infer.py --model_endpoint http://127.0.0.1:8000/custom-model
```