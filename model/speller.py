import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from model.beam import Beam
from .attention import MultiHeadAttention

supported_rnns = {
    'lstm': nn.LSTM,
    'gru': nn.GRU,
    'rnn': nn.RNN
}


class Speller(nn.Module):
    r"""
    Converts higher level features (from listener) into output utterances
    by specifying a probability distribution over sequences of characters.

    Args:
        n_class (int): the number of classfication
        max_length (int): a maximum allowed length for the sequence to be processed
        hidden_dim (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        n_layers (int, optional): number of recurrent layers (default: 1)
        rnn_type (str, optional): type of RNN cell (default: gru)
        dropout_p (float, optional): dropout probability for the output sequence (default: 0)
        k (int) : size of beam

    Inputs: inputs, listener_outputs, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **listener_outputs** (batch, seq_len, hidden_dim): tensor with containing the outputs of the listener.
          Used for attention mechanism (default is `None`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).

    Returns: y_hats, logits
        - **y_hats** (batch, seq_len): predicted y values (y_hat) by the model
        - **logits** (batch, seq_len, n_class): predicted log probability by the model

    Examples::

        >>> speller = Speller(n_class, max_length, hidden_dim, sos_id, eos_id, n_layers)
        >>> y_hats, logits = speller(inputs, listener_outputs, teacher_forcing_ratio=0.90)
    """

    def __init__(self, n_class, max_length, hidden_dim, sos_id, eos_id, n_head,
                 n_layers=1, rnn_type='gru', dropout_p=0.5, device=None, k=5):

        super(Speller, self).__init__()
        assert rnn_type.lower() in supported_rnns.keys(), 'RNN type not supported.'

        self.n_class = n_class
        self.rnn_cell = supported_rnns[rnn_type]
        self.rnn = self.rnn_cell(hidden_dim, hidden_dim, n_layers, batch_first=True, dropout=dropout_p).to(device)
        self.max_length = max_length
        self.eos_id = eos_id
        self.sos_id = sos_id
        self.hidden_dim = hidden_dim
        self.embedding = nn.Embedding(n_class, self.hidden_dim)
        self.n_layers = n_layers
        self.input_dropout = nn.Dropout(p=dropout_p)
        self.k = k
        self.fc = nn.Linear(self.hidden_dim, n_class)
        self.device = device
        self.attention = MultiHeadAttention(in_features=hidden_dim, dim=128, n_head=n_head)

    def forward_step(self, input_var, h_state, listener_outputs=None):
        embedded = self.embedding(input_var).to(self.device)
        embedded = self.input_dropout(embedded)

        if len(embedded.size()) == 2:
            embedded = embedded.unsqueeze(1)

        if self.training:
            self.rnn.flatten_parameters()

        output, h_state = self.rnn(embedded, h_state)
        context = self.attention(output, listener_outputs)

        predicted_softmax = F.log_softmax(self.fc(context.contiguous().view(-1, self.hidden_dim)), dim=1)
        return predicted_softmax, h_state

    def forward(self, inputs, listener_outputs, teacher_forcing_ratio=0.90, use_beam_search=False):
        batch_size = inputs.size(0)
        max_length = inputs.size(1) - 1  # minus the start of sequence symbol

        decode_outputs = list()
        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        h_state = self._init_state(batch_size)

        if use_beam_search:
            h_state = self._inflate(h_state, self.k, dim=1)
            batch_size, listener_seq_length, listener_hidden_dim = listener_outputs.size()
            listener_outputs = self._inflate(listener_outputs, self.k, dim=0)
            listener_outputs = listener_outputs.view(self.k, batch_size, listener_seq_length, listener_hidden_dim)
            listener_outputs = listener_outputs.transpose(0, 1)
            listener_outputs = listener_outputs.reshape(batch_size * self.k, listener_seq_length, listener_hidden_dim)

            beams = [Beam(self.k, self.sos_id, self.eos_id) for _ in range(batch_size)]

            for di in range(max_length):
                input_var = torch.stack([beam.current_predictions for beam in beams]).to(self.device)
                input_var = input_var.view(-1)

                predicted_softmax, h_state = self.forward_step(input_var, h_state, listener_outputs)
                predicted_softmax = predicted_softmax.view(batch_size, self.k, -1)

                for idx, beam in enumerate(beams):
                    beam.advance(predicted_softmax[idx, :])

            probs = list()

            for beam in beams:
                _, ks = beam.sort_finished(self.k)
                times, k = ks[0]
                hyp, beam_index, prob = beam.get_hyp(times, k)

                prob = torch.stack(prob)
                prob = beam.fill_empty_sequence(prob, max_length)
                probs.append(prob)

            probs = torch.stack(probs)
            probs = torch.transpose(probs, 0, 1)

            for i in range(probs.size(0)):
                decode_outputs.append(probs[i])

        else:
            if use_teacher_forcing:  # if teacher_forcing, Infer all at once
                inputs = inputs[inputs != self.eos_id].view(batch_size, -1)
                predicted_softmax, h_state = self.forward_step(inputs, h_state, listener_outputs)

                for di in range(predicted_softmax.size(1)):
                    step_output = predicted_softmax[:, di, :]
                    decode_outputs.append(step_output)

            else:
                input_var = inputs[:, 0].unsqueeze(1)

                for di in range(max_length):
                    predicted_softmax, h_state = self.forward_step(input_var, h_state, listener_outputs)
                    step_output = predicted_softmax.view(batch_size, input_var.size(1), -1).squeeze(1)
                    decode_outputs.append(step_output)
                    input_var = decode_outputs[-1].topk(1)[1]

        logits = torch.stack(decode_outputs, dim=1).to(self.device)
        y_hats = logits.max(-1)[1]

        return y_hats, logits

    def _inflate(self, tensor, n_repeat, dim):
        repeat_dims = [1] * len(tensor.size())
        repeat_dims[dim] *= n_repeat

        return tensor.repeat(*repeat_dims)

    def _init_state(self, batch_size):
        if isinstance(self.rnn, nn.LSTM):
            h_0 = torch.zeros(self.n_layers, batch_size, self.hidden_dim).to(self.device)
            c_0 = torch.zeros(self.n_layers, batch_size, self.hidden_dim).to(self.device)
            h_state = (h_0, c_0)

        else:
            h_state = torch.zeros(self.n_layers, batch_size, self.hidden_dim).to(self.device)

        return h_state
