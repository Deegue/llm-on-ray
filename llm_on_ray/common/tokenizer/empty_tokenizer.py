#
# Copyright 2023 The LLM-on-Ray Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from llm_on_ray.common.tokenizer import Tokenizer


class _EmptyTokenizer:
    def __init__(self, max_token_id):
        self.max_token_id = max_token_id

    def __len__(self):
        return self.max_token_id


class EmptyTokenizer(Tokenizer):
    def __call__(self, config):
        tokenizer_config = config.get("config")
        return _EmptyTokenizer(**tokenizer_config)
