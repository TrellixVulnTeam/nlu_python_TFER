from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import argparse
import logging
import os
import io
import re
import six
import glob
import mitie
from functools import wraps

import simplejson
from builtins import str

from flask import Blueprint, Flask, request, make_response,  json

from rasa_nlu.training_data import Message

from rasa_nlu.config import RasaNLUConfig
from rasa_nlu.data_router import DataRouter, InvalidProjectError, \
    AlreadyTrainingError
from rasa_nlu.train import TrainingException
from rasa_nlu.model import Metadata
from rasa_nlu.version import __version__
from rasa_nlu.utils import json_to_string
from rasa_nlu.utils.langconv import *
from rasa_nlu.tokenizers.jieba_tokenizer import JiebaTokenizer
from rasa_nlu.extractors.duckling_http_extractor import DucklingHTTPExtractor
from rasa_nlu.extractors.duckling_extractor import extract_value
from rasa_nlu.extractors.mitie_entity_extractor import MitieEntityExtractor

from rasa_core.agent import Agent
from rasa_core.channels.channel import UserMessage
from rasa_core.policies.keras_policy import KerasPolicy
from rasa_core.policies.memoization import MemoizationPolicy
from rasa_core.channels.console import ConsoleInputChannel
from rasa_core.channels.console import ConsoleOutputChannel
from rasa_core.interpreter import RasaNLUInterpreter

from flask_sqlalchemy import SQLAlchemy

from nlu_server.shared import db

import binascii

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Create database resources.
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://root:54027890@192.168.2.71:3306/nlu_python'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = True

db.init_app(app)

from nlu_server.model import Intent, Sentence, Entity, Prompt

def create_argparser():
    parser = argparse.ArgumentParser(description='parse incoming text')
    parser.add_argument('-c', '--config',
                        help="config file, all the command line options can "
                             "also be passed via a (json-formatted) config "
                             "file. NB command line args take precedence")
    parser.add_argument('-ce', '--config_entity',
                        help="config file, for entity")
    parser.add_argument('-e', '--emulate',
                        choices=['wit', 'luis', 'dialogflow'],
                        help='which service to emulate (default: None i.e. use '
                             'simple built in format)')
    parser.add_argument('-l', '--language',
                        choices=['de', 'en'],
                        help="model and data language")
    parser.add_argument('-m', '--mitie_file',
                        help='file with mitie total_word_feature_extractor')
    parser.add_argument('-p', '--path',
                        help="path where project files will be saved")
    parser.add_argument('--pipeline',
                        help="The pipeline to use. Either a pipeline template "
                             "name or a list of components separated by comma")
    parser.add_argument('-P', '--port',
                        type=int,
                        help='port on which to run server')
    parser.add_argument('-t', '--token',
                        help="auth token. If set, reject requests which don't "
                             "provide this token as a query parameter")
    parser.add_argument('-w', '--write',
                        help='file where logs will be saved')

    return parser

class RasaNLU(object):

    def __init__(self, config, component_builder=None, testing=False):
        logging.basicConfig(filename=config['log_file'],
                            level=config['log_level'])
        logging.captureWarnings(True)
        logger.debug("Configuration: " + config.view())

        self.config = config
        self.data_router = self._create_data_router(config, component_builder)
        self._testing = testing

    def _create_data_router(self, config, component_builder):
        return DataRouter(config, component_builder)

class RasaFileManeger(object):

    def __init__(self, file_name):
        self.file_name = file_name
        # set when parsing examples from a given intent
        self.current_intent = None
        self.common_examples = []
        self.entity_synonyms = []

    def nlu_load(self):
        with open(self.file_name) as json_data:
            data = json.load(json_data)
        self.common_examples = data.get("rasa_nlu_data").get("common_examples")
        self.entity_synonyms = data.get("rasa_nlu_data").get("entity_synonyms")
        return {
            "rasa_nlu_data": {
                "common_examples": self.common_examples,
                "entity_synonyms": self.entity_synonyms
            }
        }

    def nlu_save(self,data):
        output = open(self.file_name,'w',encoding='utf-8')
        output.write(data)
        output.close()

class EntityWeb(object):

    def data_router(self,rasaNLU):
        entity_webhook = Blueprint('entity_webhook', __name__)
        data_router = rasaNLU.data_router
        config = rasaNLU.config
        feature_extractor = mitie.total_word_feature_extractor(config["mitie_file"])

        @entity_webhook.route("/")
        def main():
            return make_response(open('nlu_server/templates/MainPage.html').read())
        
        @entity_webhook.route("/parse", methods=['POST'])
        def parse_get():
            payload = request.json
            text = payload.get("message", None)
            print(text)
            request_params = {}
            request_params['q'] = text
            data = data_router.extract(request_params)
            print(data)
            try:
                response = data_router.parse(data)
                return (json_to_string(response))
            except InvalidProjectError as e:
                return (json_to_string({"error": "{}".format(e)}))
            except Exception as e:
                logger.exception(e)
                return (json_to_string({"error": "{}".format(e)}))

        @entity_webhook.route("/model_list", methods=['POST'])
        def project_list():
            payload = request.json
            project_name = payload.get("project_name", None)
            if project_name is None:
                project_name = "system"
            list_projects = [fn
                for fn in glob.glob(os.path.join(config['path']+"/"+project_name, '*'))
                if os.path.isdir(fn)]
            return str(list_projects)
        
        @entity_webhook.route("/entity_get", methods=['POST'])
        def entity_get():
            payload = request.json
            text = payload.get("message", None)
            model_dir = payload.get("model_dir", None)
            model_metadata = Metadata.load(model_dir)
            print(str(model_metadata))
            output_attributes = {"entities": []}
            message = Message(text, output_attributes, time=None)
            tokens = JiebaTokenizer().tokenize(text)
            # token_strs = [token.text for token in tokens]
            # message.set("tokens",token_strs)
            extracted = []
            duckling = DucklingHTTPExtractor(config["duckling_http_url"],
                                     config["language"],
                                     config["locale"],
                                     config["duckling_dimensions"])
            matches = duckling._duckling_parse(text)
            relevant_matches = duckling._filter_irrelevant_matches(matches)
            value_list = []
            for match in relevant_matches:
                value = extract_value(match)
                if value not in value_list:
                    entity = {
                        "start": match["start"],
                        "end": match["end"],
                        "value": match["body"],
                        "data": value,
                        "additional_info": match["value"],
                        "entity": match["dim"]}
                    extracted.append(entity)
                    value_list.append(value)
            extracted = duckling.add_extractor_name(extracted)
            message.set("entities",
                    message.get("entities", []) + extracted)
            entity_extractor_file = os.path.join(model_dir+"/nlu", model_metadata.get("entity_extractor_mitie"))
            extractor = mitie.named_entity_extractor(entity_extractor_file)
            ents = MitieEntityExtractor(extractor).extract_entities(text, tokens, feature_extractor)
            extracted = MitieEntityExtractor(extractor).add_extractor_name(ents)
            message.set("entities", message.get("entities", []) + extracted, add_to_output=True)
            entities_output = []
            value_list = []
            for entity in message.get("entities"):
                if entity["value"] not in value_list:
                    entity_output = {"tag":entity["entity"],
                    "extractor":entity.get("extractor"),
                    "color":"#1E90FF",
                    "string":entity["value"]}
                    entities_output.append(entity_output)
                    value_list.append(entity["value"])
            output = {"code":1, "text": text, "entities": entities_output}
            return (json_to_string(output))

        @entity_webhook.route("/entity_save", methods=['POST'])
        def entity_save():
            payload = request.json
            text = payload.get("message", None)
            new_common_examples = payload.get("entities", list())
            entities = []
            for new_common_example in new_common_examples:
               if new_common_example.get("extractor").find("ner_mitie") >= 0:
                    entity = new_common_example.get("tag")
                    value = new_common_example.get("string")
                    start = text.find(value)
                    end = start + len(value)
                    entity_json = {
                        "start":start,
                        "end":end,
                        "entity":entity,
                        "value":value
                    }
                    print(entity_json)
                    entities.append(entity_json)
            new_common = {"text": text, "entities":entities}
            mannger =RasaFileManeger(config["data"])
            rasa_json = mannger.nlu_load().get("rasa_nlu_data")
            comment_examples = rasa_json.get("common_examples", list())
            comment_examples.append(new_common)
            print("comment_examples_add:"+str(comment_examples))
            entity_synonyms = rasa_json.get("entity_synonyms", None)
            new_json = {
                "rasa_nlu_data": {
                "common_examples": comment_examples,
                "entity_synonyms": []
                }
            }
            mannger.nlu_save(json_to_string(new_json))
            response = {"code":1, "seccess": True}
            return (json_to_string(response))

        @entity_webhook.route("/entity_mitie_train", methods=['POST'])
        def entity_mitie_train():
            mannger =RasaFileManeger(config["data"])
            data = mannger.nlu_load()
            comment_examples = data['rasa_nlu_data'].get("common_examples", list())
            mitie_ner = None
            trainer = mitie.ner_trainer(config["mitie_file"])
            trainer.num_threads = config["num_threads"]
            found_one_entity = False
            for example in comment_examples:
                text = example.get("text")
                tokens = JiebaTokenizer().tokenize(text)
                print([t.text for t in tokens])
                sample = mitie.ner_training_instance([Converter('zh-hans').convert(t.text) for t in tokens])
                for entity in example.get("entities", list()):
                    try:
                        # if the token is not aligned an exception will be raised
                        start, end = MitieEntityExtractor.find_entity(entity, text, tokens)
                    except ValueError as e:
                        logger.warning("Example skipped: {}".format(str(e)))
                        continue
                    try:
                        # mitie will raise an exception on malicious input - e.g. on overlapping entities
                        sample.add_entity(list(range(start, end)), entity["entity"])
                    except Exception as e:
                        logger.warning("Failed to add entity example '{}' of sentence '{}'. Reason: {}".format(
                            str(e), str(text), e))
                        continue
                    found_one_entity = True
                trainer.add(sample)
            # Mitie will fail to train if there is not a single entity tagged
            payload = request.json
            model_dir = payload.get("model_dir", None)
            entity_extractor_file = os.path.join(model_dir+"/nlu", "entity_extractor.dat")
            # mitie_ner = mitie.named_entity_extractor(entity_extractor_file)
            if found_one_entity:
                mitie_ner = trainer.train()
            mitie_ner.save_to_disk(entity_extractor_file, pure_model=True)    
            response = {"code":1, "seccess": True}
            return (json_to_string(response))


        return entity_webhook

class IntentWeb(object):

    def data_router(self,rasaNLU):
        intent_webhook = Blueprint('intent_webhook', __name__)
        config = rasaNLU.config

        @intent_webhook.route("/intent_save", methods=['POST'])
        def intent_save():
            payload = request.json
            bot_id = payload.get("bot_id", None)
            intent = payload.get("intent", None)
            entities = payload.get("entities", list())
            sentences = payload.get("sentences", list())
            confirm_prompts = payload.get("confirm_prompt", list())
            response_prompts = payload.get("response_prompt", list())

            intent_db = Intent()
            intent_db.intent_id = generate_key_generator()
            intent_db.intent_name = intent
            intent_db.bot_id = bot_id

            sentences_list = []
            for sentence in sentences:
                entity_list = []
                entities_save = []
                ent_save = False
                for ent in entities:
                    value = ent.get("value")
                    entity = ent.get("entity")
                    entity_type = ent.get("entity_type")
                    entity_prompts = ent.get("entity_prompt", list())
                    entity_db = Entity()
                    if sentence.find(value) >= 0:
                        if entity_type.find("user") >= 0:
                            ent_save = True
                            entity_save ={
                                "start": sentence.find(value),
                                "end": sentence.find(value)+len(value),
                                "value": value,
                                "entity": entity
                            }
                            entities_save.append(entity_save)
                        entity_db.entity_id = generate_key_generator()
                        entity_db.value = value
                        entity_db.entity = entity
                        entity_db.entity_type = entity_type
                        entity_db.start_sentence = sentence.find(value)
                        entity_db.end_sentence = sentence.find(value)+len(value)
                        entity_prompt_list = []
                        for entity_prompt in entity_prompts:
                            prompt_text = entity_prompt.get("prompt_text")
                            action_type = entity_prompt.get("action_type")
                            entity_prompt_db = Prompt()
                            entity_prompt_db.prompt_id = generate_key_generator()
                            entity_prompt_db.prompt_text = prompt_text
                            entity_prompt_db.prompt_type = "entity"
                            entity_prompt_db.action_type = action_type
                            entity_prompt_list.append(entity_prompt_db)
                        entity_db.prompt = entity_prompt_list
                        entity_list.append(entity_db)
                sentences_db = Sentence()
                sentences_db.sentence_id = generate_key_generator()
                sentences_db.sentence = sentence
                sentences_db.entity = entity_list
                sentences_list.append(sentences_db)
                if ent_save is True:
                    save_json ={
                        "text":sentence,
                        "intent":intent,
                        "entities":entities_save
                    }
                    mannger =RasaFileManeger(config["data"])
                    rasa_json = mannger.nlu_load().get("rasa_nlu_data")
                    comment_examples = rasa_json.get("common_examples", list())
                    comment_examples.append(save_json)

            intent_db.sentence = sentences_list
            
            prompt_list = []
            for confirm_prompt in confirm_prompts:
                confirm_prompt_text = confirm_prompt.get("prompt_text")
                action_type = entity_prompt.get("action_type")
                confirm_prompt_db = Prompt()
                confirm_prompt_db.prompt_id = generate_key_generator()
                confirm_prompt_db.prompt_text = confirm_prompt_text
                confirm_prompt_db.prompt_type = "confirm"
                confirm_prompt_db.action_type = action_type
                prompt_list.append(confirm_prompt_db)
            for response_prompt in response_prompts:
                response_prompt_text = response_prompt.get("prompt_text")
                action_type = entity_prompt.get("action_type")
                response_prompt_db = Prompt()
                response_prompt_db.prompt_id = generate_key_generator()
                response_prompt_db.prompt_text = response_prompt_text
                response_prompt_db.prompt_type = "response"
                response_prompt_db.action_type = action_type
                prompt_list.append(response_prompt_db)

            intent_db.prompt = prompt_list


            db.session.add(intent_db)
            db.session.commit()
            response = {"code":1, "seccess": True}
            return (json_to_string(response))


        @intent_webhook.route("/intent_list", methods=['POST'])
        def intent_list():
            payload = request.json
            bot_id = payload.get("bot_id", None)
            intent_db = Intent()
            intent_bots = intent_db.query.filter_by(bot_id = bot_id).all()
            
            intent_list=[]
            for int_bot in intent_bots:
                intent_id = int_bot.intent_id
                intent = int_bot.intent_name
                intent={
                    "intent_id":intent_id,
                    "intent":intent
                }
                intent_list.append(intent)
            output = {"intent_list":intent_list}
            return (json_to_string(output))

        @intent_webhook.route("/intent_get", methods=['POST'])
        def intent_get():
            payload = request.json
            intent_id = payload.get("intent_id", None)
            intent_db = Intent()
            intents = intent_db.query.filter_by(intent_id = intent_id).first()

            intent_id = intents.intent_id
            intent = intents.intent_name
            sentences = intents.sentence
            prompts = intents.prompt
            
            sent_list=[]
            for sent in sentences:
                sentence_id = sent.sentence_id
                sentence_text = sent.sentence
                ent_list=[]
                for ent in sent.entity:
                    entity_id = ent.entity_id
                    entity_text = ent.entity
                    value = ent.value
                    entity_type = ent.entity_type
                    end = ent.end_sentence
                    start = ent.start_sentence
                    prom_list = []
                    for prom in ent.prompt:
                        prompt_id = prom.prompt_id
                        prompt_text = prom.prompt_text
                        prompt_type = prom.prompt_type
                        action_type = prom.action_type
                        ent_prompt_json = {
                            "prompt_id":prompt_id,
                            "prompt_text":prompt_text,
                            "prompt_type":prompt_type,
                            "action_type":action_type
                        }
                        prom_list.append(ent_prompt_json)
                    ent_json={
                        "entity_id":entity_id,
                        "entity":entity_text,
                        "entity_type":entity_type,
                        "value":value,
                        "start":start,
                        "end":end,
                        "prompt":prom_list
                    }
                    ent_list.append(ent_json)
                sent_json={
                    "sentence_id":sentence_id,
                    "sentence":sentence_text,
                    "entities":ent_list
                }
                sent_list.append(sent_json)

            prompt_list=[]
            for prompt in prompts:
                prompt_id = prompt.prompt_id
                prompt_text = prompt.prompt_text
                prompt_type = prompt.prompt_type
                action_type = prom.action_type
                prompt_json = {
                    "prompt_id":prompt_id,
                    "prompt_text":prompt_text,
                    "prompt_type":prompt_type,
                    "action_type":action_type
                }
                prompt_list.append(prompt_json)
            output={
                "intent_id":intent_id,
                "intent":intent,
                "sentence":sent_list,
                "prompt":prompt_list
            }
            return (json_to_string(output))

        @intent_webhook.route("/binding_node_intent", methods=['POST'])
        def binding_node_intent():
            payload = request.json
            node_id = payload.get("node_id", None)
            flow_id = payload.get("flow_id", None)
            intent_id = payload.get("intent_id", None)
            intent_db = Intent()
            intents = intent_db.query.filter_by(intent_id = intent_id).first()
            
            response = {}
            if intents is not None:
                intents.node_id = node_id
                intents.flow_id = flow_id
                db.session.commit()

                response = {"code":1, "seccess": True}
            else:
                response = {"code":-1, "seccess": False}

            return (json_to_string(response))


        @intent_webhook.route("/intent_train", methods=['POST'])
        def intent_train():
            payload = request.json
            bot_id = payload.get("bot_id", None)
            flow_id = payload.get("flow_id", None)
            node_list = payload.get("nodeList", list())
            intent_db = Intent()
            intent_bots = intent_db.query.filter_by(bot_id = bot_id, flow_id = flow_id).all()
            response={}
            if intent_bots is not None:
                #nlu_train
                from rasa_nlu.converters import load_data
                from rasa_nlu.config import RasaNLUConfig
                from rasa_nlu.model import Trainer
                import yaml

                training_data = load_data(config["data"])
                trainer = Trainer(config)
                trainer.train(training_data)
                trainer.persist(config["path"], fixed_model_name="nlu_system")

                #DM
                intent_list=[]
                template_list=[]
                entity_list=[]
                slot_list=[]
                action_list=[]
                for int_bot in intent_bots:
                    intent = int_bot.intent_name
                    intent_list.append(intent)
                    for prompt in int_bot.prompt:
                        prompt_json ={
                            prompt.action_type+"_"+prompt.prompt_id:prompt.prompt_text
                        }
                        action_list.append(prompt.action_type+"_"+prompt.prompt_id)
                        template_list.append(prompt_json)
                    for sent in int_bot.sentence:
                        for ent in sent.entity:
                            entity_list.append(ent.entity)
                            slot_json ={
                                ent.entity:{
                                    "type": "text"
                                }
                            }
                            slot_list.append(slot_json)
                            for prompt in ent.prompt:
                                prompt_json ={
                                    prompt.action_type+"_"+prompt.prompt_id:prompt.prompt_text
                                }
                                template_list.append(prompt_json)
                yaml_json = {
                    "slots":slot_list,
                    "intents":intent_list,
                    "entities":entity_list,
                    "templates":template_list,
                    "actions":action_list
                }

                ff = open(config["dm_data_path"]+'domain.yaml', 'w+')
                yaml_dump = yaml.dump(yaml_json, default_flow_style=False)     
                ff.write(yaml_dump)  
                ff.close()

                story_list=[]
                for node in node_list:
                    if node is not None:
                        node_id = node.get("node_id")
                        story_json = convertdbToStory(node_id)
                        story_list.append(story_json)
                        node_link_list = node.get("nodeLinkList",list())
                        while node_link_list is not []:
                            sub_story = []
                            for node_link in node_link_list:
                                target_node = node_link.get("targetNode")
                                node_id = target_node.get("node_id")
                                node_link_list = target_node.get("nodeLinkList")
                                story_json = convertdbToStory(node_id)
                                sub_story.append(story_json)
                            story_list.append(sub_story)

                ff = open(config["dm_data_path"]+'stories.md', 'w+')
                for story in story_list:
                    stories = story.get("story", list())
                    story_said = None
                    if len(stories) is 1:
                        intent_text = stories.get("intent")
                        story_said+= "* " + intent_text + "\n"
                        prompt_list = stories.get("prompt_list")
                        for prompt in prompt_list:
                            prompt_id = prompt.get("prompt_id")
                            action_type = prompt.get("action_type")
                            story_said+= "    - " + action_type+"_"+prompt_id + "\n"
                    # else:
                

                agent = Agent(config["dm_data_path"]+'domain.yaml', policies=[MemoizationPolicy(), Policy()])

                agent.train(
                        config["dm_data_path"]+'stories.md',
                        max_history=5,
                        epochs=1000,
                        batch_size=400,
                        validation_split=0.2
                )

                agent.persist(model_path)
                            

                response = {"code":1, "seccess": True}
            else:
                response = {"code":-1, "seccess": False}
            return (json_to_string(response))



        return intent_webhook

class ChatWeb(object):

    def data_router(self,rasaNLU):
        chat_webhook = Blueprint('chat_webhook', __name__)
        config = rasaNLU.config

        @chat_webhook.route("/chat", methods=['POST'])
        def chat():
            payload = request.json
            model_dir = payload.get("model_dir", None)
            sender_id = payload.get("sender", None)
            text = payload.get("message", None)
            interpreter = RasaNLUInterpreter(model_dir+"/nlu")
            agent = Agent.load(model_dir+"/dialogue", interpreter=interpreter)
            agent.handle_channel(ConsoleInputChannel())
            
            message = ConsoleOutputChannel().send_custom_message()
            return jsonify(message)


        return chat_webhook

def generate_key_generator():
    return binascii.hexlify(os.urandom(16)).decode()

def convertdbToStory(node_id):
    import itertools
    intent_node_db = Intent()
    intent_node = intent_node_db.query.filter_by(node_id = node_id).first()
    intent_name = intent_node.intent_name
    node_intent_list=[]
    for sent in intent_node.sentence:
        entity_list = sent.entity
        entity_json = {}
        for index in range(1,len(entity_list)):
            if index == len(entity_list):
                for story_ent in entity_list:
                    entity_json.update({story_ent.entity:story_ent.value})
                intent = intent_name + entity_json
                prompt_list = intent_node.prompt
                node_json ={
                    "intent": intent,
                    "prompt_list":prompt_list
                }
                node_intent_list.append(node_json)
                break
            else:
                story_ent_list = list(itertools.permutations(entity_list[0:index]))
                for story_ent in story_ent_list:
                    entity_json.update({story_ent.entity:story_ent.value})
                intent = intent_name + entity_json
                prompt_list = story_ent_list[-1].prompt
                node_json ={
                    "intent": intent,
                    "prompt_list":prompt_list
                }
                node_intent_list.append(node_json)
    story_json = {
        "story":node_intent_list
    }
    return story_json

class Policy(KerasPolicy):
    def model_architecture(self, num_features, num_actions, max_history_len):
        """Build a Keras model and return a compiled model."""
        from keras.layers import LSTM, Activation, Masking, Dense
        from keras.models import Sequential

        n_hidden = 32  # size of hidden layer in LSTM
        # Build Model
        batch_shape = (None, max_history_len, num_features)

        model = Sequential()
        model.add(Masking(-1, batch_input_shape=batch_shape))
        model.add(LSTM(n_hidden, batch_input_shape=batch_shape))
        # model.add(Dense(input_dim=n_hidden, output_dim=num_actions))
        model.add(Activation('softmax'))

        model.compile(loss='categorical_crossentropy',
                      optimizer='adam',
                      metrics=['accuracy'])

        logger.debug(model.summary())
        return model


if __name__ == "__main__": 
    arg_parser = create_argparser()
    cmdline_args = {key: val
                    for key, val in list(vars(arg_parser.parse_args()).items())
                    if val is not None}
    rasa_nlu_config = RasaNLUConfig(
            cmdline_args.get("config"), os.environ, cmdline_args)
    rasa = RasaNLU(rasa_nlu_config)
    entity_channel = EntityWeb()
    intent_channel = IntentWeb()
    chat_channel = ChatWeb()
    app.register_blueprint(entity_channel.data_router(rasa), url_prefix='/ai_entity')
    app.register_blueprint(intent_channel.data_router(rasa), url_prefix='/ai_intent')
    app.register_blueprint(chat_channel.data_router(rasa), url_prefix='/chat')
    from gevent.wsgi import WSGIServer
    http_server = WSGIServer((rasa_nlu_config['server_ip'], rasa_nlu_config['port']), app)
    http_server.serve_forever()