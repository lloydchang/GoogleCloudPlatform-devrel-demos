# Copyright 2023 Google LLC
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     https://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tensorflow as tf
from tensorflow import keras
import tensorflow_text
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.ml.inference.tensorflow_inference import TFModelHandlerTensor
from apache_beam.ml.inference.base import PredictionResult
from apache_beam.ml.inference.base import RunInference
from apache_beam.ml.inference.base import KeyedModelHandler
import argparse

class extendTFModelHandlerTensor (beam.ml.inference.tensorflow_inference.TFModelHandlerTensor):
    # We currently need to overload the method 
    # This will be unnecessary when https://github.com/apache/beam/pull/26548 goes live
    def load_model(self) -> tf.Module:
        return tf.keras.models.load_model(self._model_uri,compile=False)

class tag_with_key(beam.DoFn):
    # In this pardo, we key our elements using the attributes of the message
    def process(self, element):
        yield (element.attributes["userid"],(element.data).decode('UTF-8'))

class flag_for_toxic(beam.DoFn):
    def process(self, element):
        # Parsing the output of the inference
        # We need to pull out the tensor and conver it to numpy 
        # Note: for sake of brevity, we've used a hardcoded method
        # In production and for good practice you'll want to use the PredictionResult object
        tox_level = element[1][1].numpy().item()
        # We've put an arbitrary value to determine toxicity
        # This value is something you'll need to align with the model
        # The arbitrary value is just for demonstration purposes
        if tox_level > -0.5:
            yield ("not",element)
        else:
            yield ("nice",element)

def run(project_id, gaming_model_location, movie_model_location, pipeline_args):
    pipeline_options = PipelineOptions(
        pipeline_args, save_main_session=True)
    
    # We are using a topic for input
    # Pub/Sub IO will automatically create a subscription for us
    input_topic = "projects/{}/topics/tox-input".format(project_id)
    output_topic = "projects/{}/topics/tox-output".format(project_id)
    output_bigquery = "{}:demo.tox".format(project_id)

    with beam.Pipeline(options=pipeline_options) as p:

        # We first read from Pub/Sub
        # Because it's a streaming pipeline, we need to apply a window for the join
        # Finally we key the data so we can join it back after the A/B test
        read_from_pubsub = (
            p 
            | "Read from PubSub" >> beam.io.ReadFromPubSub(topic=input_topic,with_attributes=True)
            # In this particular example, we aren't worried about an accurate window
            # If uniqueness is an issue, we can switch to using message ID of each message 
            # The message ID will be unique and will ensure uniqueness 
            | "Window data" >> beam.WindowInto(beam.window.FixedWindows(0.1))
            | "Key up input" >> beam.ParDo(tag_with_key())
        )

        # Load the model into a handler
        # We use KeyedModelHandler here to automatically handle the incoming keys
        # It also returns the key so you can preserve the key and use it after the prediction
        gaming_model_handler = KeyedModelHandler(extendTFModelHandlerTensor(gaming_model_location))

        # Use the handler to perform inference
        # Note that the gaming toxicity score is based on "toxic or not"
        # The scale differs from the movie model
        gaming_inference = (
            read_from_pubsub 
            | "Perform gaming inference" >> RunInference(gaming_model_handler)
        )

        # Flag the values so we can determine if toxic or not
        nice_or_not = (
            gaming_inference 
            | beam.ParDo(flag_for_toxic())
        )
        
        # Print to screen so we can see the results
        nice_or_not | beam.Map(print)

        # Filter, if toxic then write to Pub/Sub
        # "Not" denotes not nice
        not_filter = nice_or_not | beam.Filter(lambda outcome: outcome[0] == "not")
        
        # Write to Pub/Sub
        _ = (not_filter 
            | "Convert to bytestring" >> beam.Map(lambda element: bytes(str(element[1]),"UTF-8"))
            | beam.io.WriteToPubSub(topic=output_topic)
        )

        # Step 1: Create the model handler 
        # Load the model into a handler
        movie_model_handler = KeyedModelHandler(extendTFModelHandlerTensor(movie_model_location))

        # Step 2: Submit the input into the model for a result
        # Note that the movie score differ in scoring
        # "negative" would mean negative values
        # "postivie" would mean positive values
        # Use the handler to perform inference
        movie_inference = (
            read_from_pubsub 
            | "Perform movie inference" >> RunInference(movie_model_handler)
        )

        # Step 3: Join your results together
        # We join up the data so we can compare the values later
        joined = (
            ({'gaming': gaming_inference, 'movie': movie_inference})
            | 'Join' >> beam.CoGroupByKey()
        )

        # Step 4: Transform your joined results into a string
        # Simple string schema - normally not recommended 
        # For brevity sake, we convert to a single string
        schema = {'fields': [
            {'name': 'data_col', 'type': 'STRING', 'mode': 'NULLABLE'}]}
        
        # Step 6: Join your results together
        # Write to BigQuery
        # We're converting to the simple string to insert
        _ = (
            joined
            | "Convert to string" >> beam.Map(lambda element: {"data_col":str(element)})
            | beam.io.gcp.bigquery.WriteToBigQuery(
                method=beam.io.gcp.bigquery.WriteToBigQuery.Method.STREAMING_INSERTS,
                table=output_bigquery,
                schema=schema
            )
        )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--project_id',
        dest='project_id',
        required=True,
        help=('project id'))
    parser.add_argument(
        '--gaming',
        dest='gaming_loc',
        required=True,
        help=('location of gaming model'))
    parser.add_argument(
        '--movie',
        dest='movie_loc',
        required=True,
        help=('location of movie model'))
    known_args, pipeline_args = parser.parse_known_args()
    run(known_args.project_id, known_args.gaming_loc, known_args.movie_loc, pipeline_args)


