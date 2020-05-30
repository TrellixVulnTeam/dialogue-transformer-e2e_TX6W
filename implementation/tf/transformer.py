import tensorflow as tf
import time
import numpy as np
from reader import *
import os
import warnings
import metric

try:
    import neptune
except ImportError:
    warnings.warn('neptune module is not installed (used for logging)', ImportWarning)

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def get_angles(pos, i, d_model):
  angle_rates = 1 / np.power(10000, (2 * (i//2)) / np.float32(d_model))
  return pos * angle_rates


def positional_encoding(position, d_model):
    angle_rads = get_angles(np.arange(position)[:, np.newaxis],
                            np.arange(d_model)[np.newaxis, :],
                            d_model)

    # apply sin to even indices in the array; 2i
    angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])

    # apply cos to odd indices in the array; 2i+1
    angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])

    pos_encoding = angle_rads[np.newaxis, ...]

    return tf.cast(pos_encoding, dtype=tf.float32)


def scaled_dot_product_attention(q, k, v, mask):
    """Calculate the attention weights.
    q, k, v must have matching leading dimensions.
    k, v must have matching penultimate dimension, i.e.: seq_len_k = seq_len_v.
    The mask has different shapes depending on its type(padding or look ahead)
    but it must be broadcastable for addition.

    Args:
      q: query shape == (..., seq_len_q, depth)
      k: key shape == (..., seq_len_k, depth)
      v: value shape == (..., seq_len_v, depth_v)
      mask: Float tensor with shape broadcastable
            to (..., seq_len_q, seq_len_k). Defaults to None.

    Returns:
      output, attention_weights
    """

    matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

    # scale matmul_qk
    dk = tf.cast(tf.shape(k)[-1], tf.float32)
    scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

    # add the mask to the scaled tensor.
    if mask is not None:
        scaled_attention_logits += (mask * -1e9)

        # softmax is normalized on the last axis (seq_len_k) so that the scores
    # add up to 1.
    attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)

    output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)

    return output, attention_weights


np.set_printoptions(suppress=True)

def create_padding_mask(seq):
    seq = tf.cast(tf.math.equal(seq, 0), tf.float32)

    # add extra dimensions to add the padding
    # to the attention logits.
    return seq[:, tf.newaxis, tf.newaxis, :]  # (batch_size, 1, 1, seq_len)


def create_look_ahead_mask(size):
    mask = 1 - tf.linalg.band_part(tf.ones((size, size)), -1, 0)
    return mask  # (seq_len, seq_len)


def create_masks(inp, tar):
    # Encoder padding mask
    enc_padding_mask = create_padding_mask(inp)

    # Used in the 2nd attention block in the decoder.
    # This padding mask is used to mask the encoder outputs.
    dec_padding_mask = create_padding_mask(inp)

    # Used in the 1st attention block in the decoder.
    # It is used to pad and mask future tokens in the input received by
    # the decoder.
    look_ahead_mask = create_look_ahead_mask(tf.shape(tar)[1])
    dec_target_padding_mask = create_padding_mask(tar)
    combined_mask = tf.maximum(dec_target_padding_mask, look_ahead_mask)

    return enc_padding_mask, combined_mask, dec_padding_mask


class MultiHeadAttention(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads):
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model

        assert d_model % self.num_heads == 0

        self.depth = d_model // self.num_heads

        self.wq = tf.keras.layers.Dense(d_model)
        self.wk = tf.keras.layers.Dense(d_model)
        self.wv = tf.keras.layers.Dense(d_model)

        self.dense = tf.keras.layers.Dense(d_model)

    def split_heads(self, x, batch_size):
        """Split the last dimension into (num_heads, depth).
        Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
        """
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, v, k, q, mask):
        batch_size = tf.shape(q)[0]

        q = self.wq(q)  # (batch_size, seq_len, d_model)
        k = self.wk(k)  # (batch_size, seq_len, d_model)
        v = self.wv(v)  # (batch_size, seq_len, d_model)

        q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
        k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
        v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)

        # scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
        # attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
        scaled_attention, attention_weights = scaled_dot_product_attention(
            q, k, v, mask)

        scaled_attention = tf.transpose(scaled_attention,
                                        perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

        concat_attention = tf.reshape(scaled_attention,
                                      (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

        output = self.dense(concat_attention)  # (batch_size, seq_len_q, d_model)

        return output, attention_weights, scaled_attention


def point_wise_feed_forward_network(d_model, dff):
  return tf.keras.Sequential([
      tf.keras.layers.Dense(dff, activation='relu', input_shape=(None, d_model)),  # (batch_size, seq_len, dff)
      tf.keras.layers.Dense(d_model)  # (batch_size, seq_len, d_model)
  ])


def read_embeddings(reader, embeddings_file="data/glove.6B.{}d.txt", embedding_size=50):
    """
    :param reader: a dialogue dataset reader, where we will get words mapped to indices
    :param embeddings_file: file path for glove embeddings
    :return: dictionary of indices mapped to their glove embeddings
    """
    vocab_to_index = {reader.vocab.decode(id): id for id in range(cfg.vocab_size)}
    embedding_matrix = np.zeros((cfg.vocab_size + 1, embedding_size))
    embeddings_file = embeddings_file.format(embedding_size)
    with open(embeddings_file) as infile:
        for line in infile:
            word, coeffs = line.split(maxsplit=1)
            if word in vocab_to_index:
                word_index = vocab_to_index[word]
                embedding_matrix[word_index] = np.fromstring(coeffs, 'f', sep=' ')

    return embedding_matrix


class EncoderLayer(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, dff, rate=0.1):
        super(EncoderLayer, self).__init__()

        self.mha = MultiHeadAttention(d_model, num_heads)
        self.ffn = point_wise_feed_forward_network(d_model, dff)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)

    def call(self, x, training, mask):
        attn_output, _, _ = self.mha(x, x, x, mask)  # (batch_size, input_seq_len, d_model)
        attn_output = self.dropout1(attn_output, training=training)
        out1 = self.layernorm1(x + attn_output)  # (batch_size, input_seq_len, d_model)

        ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
        ffn_output = self.dropout2(ffn_output, training=training)
        out2 = self.layernorm2(out1 + ffn_output)  # (batch_size, input_seq_len, d_model)

        return out2


class DecoderLayer(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, dff, rate=0.1):
        super(DecoderLayer, self).__init__()

        self.mha1 = MultiHeadAttention(d_model, num_heads)
        self.mha2 = MultiHeadAttention(d_model, num_heads)

        self.ffn = point_wise_feed_forward_network(d_model, dff)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm3 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)
        self.dropout3 = tf.keras.layers.Dropout(rate)

    def call(self, x, enc_output, training,
             look_ahead_mask, padding_mask):
        # enc_output.shape == (batch_size, input_seq_len, d_model)

        attn1, attn_weights_block1, _ = self.mha1(x, x, x, look_ahead_mask)  # (batch_size, target_seq_len, d_model)
        attn1 = self.dropout1(attn1, training=training)
        out1 = self.layernorm1(attn1 + x)

        attn2, attn_weights_block2, scaled_attention = self.mha2(
            enc_output, enc_output, out1, padding_mask)  # (batch_size, target_seq_len, d_model)
        attn2 = self.dropout2(attn2, training=training)
        out2 = self.layernorm2(attn2 + out1)  # (batch_size, target_seq_len, d_model)

        ffn_output = self.ffn(out2)  # (batch_size, target_seq_len, d_model)
        ffn_output = self.dropout3(ffn_output, training=training)
        out3 = self.layernorm3(ffn_output + out2)  # (batch_size, target_seq_len, d_model)

        return out3, attn_weights_block1, attn_weights_block2, attn2


class Encoder(tf.keras.layers.Layer):
    def __init__(self, num_layers, d_model, num_heads, dff, input_vocab_size,
                 maximum_position_encoding, rate=0.1, embeddings_matrix=None):
        super(Encoder, self).__init__()

        self.d_model = d_model
        self.num_layers = num_layers

        if embeddings_matrix is not None:
            self.embedding = tf.keras.layers.Embedding(input_vocab_size, d_model,
                                                       embeddings_initializer=tf.keras.initializers.Constant(embeddings_matrix))
        else:
            self.embedding = tf.keras.layers.Embedding(input_vocab_size, d_model)

        self.pos_encoding = positional_encoding(maximum_position_encoding,
                                                self.d_model)

        self.enc_layers = [EncoderLayer(d_model, num_heads, dff, rate)
                           for _ in range(num_layers)]

        self.dropout = tf.keras.layers.Dropout(rate)

    def call(self, x, training, mask):
        seq_len = tf.shape(x)[1]

        # adding embedding and position encoding.
        x = self.embedding(x)  # (batch_size, input_seq_len, d_model)
        x *= tf.math.sqrt(tf.cast(self.d_model, tf.float32))
        x += self.pos_encoding[:, :seq_len, :]

        x = self.dropout(x, training=training)

        for i in range(self.num_layers):
            x = self.enc_layers[i](x, training, mask)

        return x  # (batch_size, input_seq_len, d_model)


class Decoder(tf.keras.layers.Layer):
    def __init__(self, num_layers, d_model, num_heads, dff, target_vocab_size,
                 maximum_position_encoding, rate=0.1, copynet=False, embeddings_matrix=None):
        super(Decoder, self).__init__()
        self.target_vocab_size = target_vocab_size
        self.copynet = copynet
        self.d_model = d_model
        self.num_layers = num_layers

        if embeddings_matrix is not None:
            self.embedding = tf.keras.layers.Embedding(target_vocab_size, d_model,
                                                       embeddings_initializer=tf.keras.initializers.Constant(embeddings_matrix))
        else:
            self.embedding = tf.keras.layers.Embedding(target_vocab_size, d_model)

        self.pos_encoding = positional_encoding(maximum_position_encoding, d_model)

        self.dec_layers = [DecoderLayer(d_model, num_heads, dff, rate)
                           for _ in range(num_layers)]
        self.dropout = tf.keras.layers.Dropout(rate)

        if self.copynet:
            self.copy_network = tf.keras.Sequential([
                tf.keras.layers.Dense(dff, activation='relu', input_shape=(None, d_model)),
                tf.keras.layers.Dense(1)])  # (batch_size, seq_len, d_model)
            self.gen_prob = tf.keras.layers.Dense(1, activation="sigmoid")

    def call(self, x, enc_output, training,
             look_ahead_mask, padding_mask):
        seq_len = tf.shape(x)[1]
        attention_weights = {}
        initial_input = x

        x = self.embedding(x)  # (batch_size, target_seq_len, d_model)
        embedded = x
        x *= tf.math.sqrt(tf.cast(self.d_model, tf.float32))
        x += self.pos_encoding[:, :seq_len, :]

        x = self.dropout(x, training=training)

        for i in range(self.num_layers):
            x, block1, block2, attn = self.dec_layers[i](x, enc_output, training,
                                                   look_ahead_mask, padding_mask)

            attention_weights['decoder_layer{}_block1'.format(i + 1)] = block1
            attention_weights['decoder_layer{}_block2'.format(i + 1)] = block2

        if self.copynet:
            p_gen = self.gen_prob(x)
            copy_distribution = self.copy_network(attn)
            try:
                copy_distribution = tf.squeeze(copy_distribution, axis=1)
            except tf.errors.InvalidArgumentError:
                copy_distribution = tf.squeeze(copy_distribution)
            copy_probs = tf.nn.softmax(copy_distribution)
            if copy_probs.shape.ndims == 1:
                copy_probs = tf.expand_dims(copy_probs, axis=0)
            i1, i2 = tf.meshgrid(tf.range(initial_input.shape[0]),
                                 tf.range(initial_input.shape[1]), indexing="ij")
            i1 = tf.tile(i1[:, :, tf.newaxis], [1, 1, 1])
            i2 = tf.tile(i2[:, :, tf.newaxis], [1, 1, 1])
            # Create final indices
            idx = tf.stack([i1, i2, tf.expand_dims(initial_input, axis=2)], axis=-1)
            # Output shape
            to_shape = [initial_input.shape[0], initial_input.shape[1], self.target_vocab_size]
            # Get scattered tensor
            output = tf.scatter_nd(idx, tf.expand_dims(copy_probs, axis=2), to_shape)
            copy_logits = tf.reduce_sum(output, axis=1)
        else:
            p_gen = 0.
            copy_logits = tf.zeros((initial_input.shape[0], self.target_vocab_size), dtype=x.dtype)
        copy_logits = tf.tile(tf.expand_dims(copy_logits, axis=1), [1, x.shape[1], 1])
        # x.shape == (batch_size, target_seq_len, d_model)
        return x, attention_weights, p_gen, copy_logits


class Transformer(tf.keras.Model):
    def __init__(self, num_layers, d_model, num_heads, dff, input_vocab_size,
                 target_vocab_size, pe_input, pe_target, rate=0.1, copynet=False, embeddings_matrix=None):
        super(Transformer, self).__init__()

        self.copynet = copynet

        self.encoder = Encoder(num_layers, d_model, num_heads, dff,
                               input_vocab_size, pe_input, rate, )

        self.response_decoder = Decoder(num_layers, d_model, num_heads, dff,
                               target_vocab_size, pe_target, rate, copynet, embeddings_matrix)

        self.bspan_decoder = Decoder(num_layers, d_model, num_heads, dff,
                               target_vocab_size, pe_target, rate, copynet, embeddings_matrix)

        self.response_final = tf.keras.layers.Dense(target_vocab_size)
        self.bspan_final = tf.keras.layers.Dense(target_vocab_size)

    def bspan(self, inp, tar, training, enc_padding_mask, look_ahead_mask, dec_padding_mask):
        enc_output = self.encoder(inp, training, enc_padding_mask)  # (batch_size, inp_seq_len, d_model)

        # dec_output.shape == (batch_size, tar_seq_len, d_model)
        dec_output, attention_weights, p_gen, copy_logits = self.bspan_decoder(
            tar, enc_output, training, look_ahead_mask, dec_padding_mask)

        bspan_output = self.response_final(dec_output)  # (batch_size, tar_seq_len, target_vocab_size)
        if self.copynet:
            bspan_output = p_gen * bspan_output + (1-p_gen) * copy_logits

        return bspan_output, attention_weights

    def response(self, inp, tar, training, enc_padding_mask, look_ahead_mask, dec_padding_mask):
        enc_output = self.encoder(inp, training, enc_padding_mask)  # (batch_size, inp_seq_len, d_model)

        # dec_output.shape == (batch_size, tar_seq_len, d_model)
        dec_output, attention_weights, p_gen, copy_logits = self.response_decoder(
            tar, enc_output, training, look_ahead_mask, dec_padding_mask)

        response_output = self.response_final(dec_output)  # (batch_size, tar_seq_len, target_vocab_size)
        if self.copynet:
            response_output = p_gen * response_output + (1-p_gen) * copy_logits

        return response_output, attention_weights


class CustomSchedule(tf.keras.optimizers.schedules.LearningRateSchedule):
    def __init__(self, d_model, warmup_steps=4000):
        super(CustomSchedule, self).__init__()

        self.d_model = d_model
        self.d_model = tf.cast(self.d_model, tf.float32)

        self.warmup_steps = warmup_steps

    def __call__(self, step):
        arg1 = tf.math.rsqrt(step)
        arg2 = step * (self.warmup_steps ** -1.5)

        return tf.math.rsqrt(self.d_model) * tf.math.minimum(arg1, arg2)


loss_object = tf.keras.losses.SparseCategoricalCrossentropy(
    from_logits=True, reduction='none')


def loss_function(real, pred):
    mask = tf.math.logical_not(tf.math.equal(real, 0))
    loss_ = loss_object(real, pred)

    mask = tf.cast(mask, dtype=loss_.dtype)
    loss_ *= mask

    return tf.reduce_sum(loss_) / tf.reduce_sum(mask)


def tensorize(id_lists):
    tensorized = tf.ragged.constant([x for x in id_lists]).to_tensor()
    return tf.cast(tensorized, dtype=tf.int32)


# TODO change these functions so that they can take tensor input and not just list
def produce_bspan_decoder_input(previous_bspan, previous_response, user_input):
    inputs =[]
    start_symbol = [cfg.vocab_size]
    for counter, (x, y, z) in enumerate(zip(previous_bspan, previous_response, user_input)):
        new_sample = start_symbol + x + y + z  # TODO concatenation should be more readable than this
        inputs.append(new_sample)
    return tensorize(inputs)


def produce_response_decoder_input(previous_bspan, previous_response, user_input, bspan, kb):
    start_symbol = [cfg.vocab_size]
    inputs = []
    for a, b, c, d, e in zip(previous_bspan, previous_response, user_input, bspan, kb):
        inputs.append(start_symbol + a + b + c + d + e)
    return tensorize(inputs)


class SeqModel:
    def __init__(self, vocab_size, num_layers=3, d_model=50, dff=512, num_heads=5, dropout_rate=0.1, copynet=False,
                 reader=None):
        self.vocab_size = vocab_size + 1
        input_vocab_size = vocab_size + 1
        target_vocab_size = vocab_size + 1

        self.learning_rate = CustomSchedule(d_model)
        self.optimizer = tf.keras.optimizers.Adam(self.learning_rate, beta_1=0.9, beta_2=0.98, epsilon=1e-9)
        self.bspan_loss = tf.keras.metrics.Mean(name='train_loss')
        self.response_loss = tf.keras.metrics.Mean(name='train_loss')
        self.bspan_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')
        self.response_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')
        self.reader = reader

        if reader:
            print("Reading pre-trained word embeddings with {} dimensions".format(d_model))
            embeddings_matrix = read_embeddings(reader, embedding_size=d_model)
        else:
            print("Initializing without pre-trained embeddings.")
            embeddings_matrix=None

        self.transformer = Transformer(num_layers, d_model, num_heads, dff,
                                  input_vocab_size, target_vocab_size,
                                  pe_input=input_vocab_size,
                                  pe_target=target_vocab_size,
                                  rate=dropout_rate, copynet=copynet, embeddings_matrix=embeddings_matrix)

    #@tf.function(input_signature=[tf.TensorSpec(shape=(None, None), dtype=tf.int32),
    #    tf.TensorSpec(shape=(None, None), dtype=tf.int32)])
    def train_step_bspan(self, inp, tar):
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)

        with tf.GradientTape() as tape:
            predictions, _ = self.transformer.bspan(inp=inp, tar=tar_inp, training=True,
                                                    enc_padding_mask=enc_padding_mask, look_ahead_mask=combined_mask,
                                                    dec_padding_mask=dec_padding_mask)

            loss = loss_function(tar_real, predictions)

        gradients = tape.gradient(loss, self.transformer.trainable_variables)
        gradients =[grad if grad is not None else tf.zeros_like(var)
                    for grad, var in zip(gradients, self.transformer.trainable_variables)]
        self.optimizer.apply_gradients(zip(gradients, self.transformer.trainable_variables))

        self.bspan_accuracy(tar_real, predictions)

    #@tf.function(input_signature=[tf.TensorSpec(shape=(None, None), dtype=tf.int32),
    #    tf.TensorSpec(shape=(None, None), dtype=tf.int32)])
    def train_step_response(self, inp, tar):
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)

        with tf.GradientTape() as tape:
            predictions, _ = self.transformer.response(inp=inp, tar=tar_inp, training=True,
                                                       enc_padding_mask=enc_padding_mask, look_ahead_mask=combined_mask,
                                                       dec_padding_mask=dec_padding_mask)
            loss = loss_function(tar_real, predictions)

        gradients = tape.gradient(loss, self.transformer.trainable_variables)
        gradients =[grad if grad is not None else tf.zeros_like(var)
                    for grad, var in zip(gradients, self.transformer.trainable_variables)]
        self.optimizer.apply_gradients(zip(gradients, self.transformer.trainable_variables))

        self.response_accuracy(tar_real, predictions)

    def train_model(self, epochs=20, log=False, max_sent=1):
        # TODO add a start token to all of these things and increase vocab size by one
        constraint_eos, request_eos, response_eos = "EOS_Z1", "EOS_Z2", "EOS_M"
        for epoch in range(epochs):
            data_iterator = self.reader.mini_batch_iterator('train')
            for iter_num, dial_batch in enumerate(data_iterator):
                previous_bspan, previous_response = None, None
                for turn_num, turn_batch in enumerate(dial_batch):
                    _, _, user, response, bspan_received, u_len, m_len, degree, _ = turn_batch.values()
                    batch_size = len(user)
                    if previous_bspan is None:
                        previous_bspan = [[self.reader.vocab.encode(constraint_eos),
                                           self.reader.vocab.encode(request_eos)] for i in range(batch_size)]
                        previous_response = [[self.reader.vocab.encode(response_eos)] for i in range(batch_size)]
                    target_bspan = tensorize([[cfg.vocab_size] + x for x in bspan_received])
                    target_response = tensorize([[cfg.vocab_size] + x for x in response])

                    bspan_decoder_input = produce_bspan_decoder_input(previous_bspan, previous_response, user)
                    response_decoder_input = produce_response_decoder_input(previous_bspan, previous_response,
                                                                            user, bspan_received, degree)
                    # TODO actually save the models, keeping track of the best one

                    # training the model
                    self.train_step_bspan(bspan_decoder_input, target_bspan)
                    self.train_step_response(response_decoder_input, target_response)

                    previous_bspan = bspan_received
                    previous_response = response
            print("Completed epoch #{} of {}".format(epoch + 1, epochs))
            self.evaluation(verbose=True, log=log, max_sent=max_sent, use_metric=True)

    def auto_regress(self, input_sequence, decoder, MAX_LENGTH=256):
        assert decoder in ["bspan", "response"]
        decoder_input = [cfg.vocab_size]
        output = tf.expand_dims(decoder_input, 0)

        end_token_id = self.reader.vocab.encode("EOS_Z2") if decoder == "bspan" else self.reader.vocab.encode("EOS_M")

        for i in range(MAX_LENGTH):
            enc_padding_mask, combined_mask, dec_padding_mask = create_masks(input_sequence, output)

            if decoder == "bspan":
                predictions, attention_weights = self.transformer.bspan(input_sequence, output, False,
                                                                        enc_padding_mask, combined_mask,
                                                                        dec_padding_mask)
            else:
                predictions, attention_weights = self.transformer.response(input_sequence, output, False,
                                                                           enc_padding_mask, combined_mask,
                                                                           dec_padding_mask)

            predictions = predictions[:, -1:, :]  # (batch_size, 1, vocab_size)
            predicted_id = tf.cast(tf.argmax(predictions, axis=-1), tf.int32)

            if predicted_id == end_token_id:
                return tf.squeeze(output, axis=0), attention_weights

            output = tf.concat([output, predicted_id], axis=-1)

        return tf.squeeze(output, axis=0), attention_weights

    def evaluate(self, previous_bspan, previous_response, user, degree):
        bspan_decoder_input = produce_bspan_decoder_input([previous_bspan], [previous_response], [user])
        predicted_bspan, _ = self.auto_regress(bspan_decoder_input, "bspan")

        response_decoder_input = produce_response_decoder_input([previous_bspan], [previous_response],
                                                                [user], [list(predicted_bspan.numpy())], [degree])
        predicted_response, _ = self.auto_regress(response_decoder_input, "response")
        return predicted_response

    def evaluation(self, mode="dev", verbose=False, log=False, max_sent=1, use_metric=False):
        dialogue_set = self.reader.dev if mode == "dev" else self.reader.test
        predictions, targets = list(), list()
        constraint_eos, request_eos, response_eos = "EOS_Z1", "EOS_Z2", "EOS_M"
        for dialogue in dialogue_set[0:max_sent]:
            previous_bspan = [self.reader.vocab.encode(constraint_eos), self.reader.vocab.encode(request_eos)]
            previous_response = [self.reader.vocab.encode(response_eos)]
            for turn in dialogue[0:max_sent]:
                dial_id, turn_num, user, response, bspan, u_len, m_len, degree = turn.values()
                response, bspan = [cfg.vocab_size] + response, [cfg.vocab_size] + bspan
                predicted_response = self.evaluate(previous_bspan, previous_response, user, degree)
                if verbose:
                    print("Predicted:", self.reader.vocab.sentence_decode(predicted_response.numpy()))
                    print("Real:", self.reader.vocab.sentence_decode(response))
                if log:
                    neptune.log_text('predicted', self.reader.vocab.sentence_decode(predicted_response.numpy()))
                    neptune.log_text('real', self.reader.vocab.sentence_decode(response))

                predictions.append(predicted_response)
                targets.append(response)
        if use_metric:
            scorer = metric.BLEUScorer()
            bleu = scorer.score(zip((self.reader.vocab.sentence_decode(p.numpy()) for p in predictions), (self.reader.vocab.sentence_decode(t) for t in targets)))
            if verbose:
                print("Bleu: {:.4f}%".format(bleu*100))
            if log:
                neptune.log_metric('bleu', bleu)
                if max_sent >=100:
                    neptune.log_metric('bleu_final', bleu)




if __name__ == "__main__":
    # TODO make the embeddings optional and clean up the reader logic in the model creation
    # TODO model saving, with parameters processing in saving and loading
    # TODO try with different setups (number of heads, number of layers)
    # TODO Evaluation output should be written/plotted/run independently
    ds = "tsdf-camrest"
    cfg.init_handler(ds)
    cfg.dataset = ds.split('-')[-1]
    reader = CamRest676Reader()
    model = SeqModel(vocab_size=cfg.vocab_size, copynet=True, reader=reader)
    model.train_model(log=False)
