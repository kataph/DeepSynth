import logging

from torch._C import dtype

from dsl import DSL
from program import Program, Function, Variable, BasicPrimitive, New
from type_system import Type, PolymorphicType, PrimitiveType, Arrow, List, UnknownType, INT, BOOL
import torch
from torch import nn
from pcfg import PCFG

device = 'cpu'

def block(input_dim, output_dimension, activation):
    return nn.Sequential(
        nn.Linear(input_dim, output_dimension),
        activation,
    )

class GlobalRulesPredictor(nn.Module):
    '''
    cfg: a cfg template
    IOEncoder: encode inputs and outputs
    IOEmbedder: embeds inputs and outputs
    size_hidden: size for hidden layers
    '''
    def __init__(self, 
        cfg, 
        IOEncoder,
        IOEmbedder,
        size_hidden,
        ):
        super(GlobalRulesPredictor, self).__init__()

        self.cfg = cfg
        self.IOEncoder = IOEncoder
        self.IOEmbedder = IOEmbedder

        self.loss = torch.nn.BCELoss(reduction='mean')
        self.optimizer = torch.optim.Adam(self.parameters(), lr=0.1)

        self.init_RuleToIndex()

        # layers
        H = IOEncoder.output_dimension * self.IOEmbedder.output_dimension
        self.hidden = nn.Sequential(
            block(H, size_hidden, nn.LeakyReLU()),
            # block(size_hidden, size_hidden, nn.LeakyReLU()),
            block(size_hidden, size_hidden, nn.LeakyReLU()),
        )
        # final layer
        self.final_layer = block(size_hidden, self.output_dimension, nn.Sigmoid())

    def init_RuleToIndex(self):
        self.output_dimension = 0

        index = 0
        # self.RuleToIndex[(S,P)] is the index position of the derivation (S,P)
        # in the final layer of the neural network
        self.RuleToIndex = {}
        for S in self.cfg.rules:
            self.output_dimension += len(self.cfg.rules[S])
            for P in self.cfg.rules[S]:
                self.RuleToIndex[(S, P)] = index
                index += 1

    def forward(self, batch_IOs):
        '''
        batch_IOs is a tensor of size
        (batch_size, IOEncoder.output_dimension, IOEmbedder.output_dimension) 
        '''
        # print("size of x", x.size())
        x = self.IOEmbedder.forward(batch_IOs)
        # print("size of x", x.size())
        x = self.hidden(x)
        # print("size of x", x.size())
        # x = torch.mean(x, -2)
        # print("size of x", x.size())
        x = self.final_layer(x)
        # print("size of x", x.size())
        return x

        # res = []
        # for x in batch_IOs:
        #     # print("size of x", x.size())
        #     x = self.IOEmbedder.forward_IOs(x)
        #     # print("size of x", x.size())
        #     x = torch.flatten(x, start_dim = 1)
        #     # print("size of x", x.size())
        #     x = self.hidden(x)
        #     # print("size of x", x.size())
        #     x = torch.mean(x, -2)
        #     # print("size of x", x.size())
        #     x = self.final_layer(x)
        #     # print("size of x", x.size())
        #     res.append(x)
        # return torch.stack(res)

    def reconstruct_grammars(self, batch_predictions):
        '''
        reconstructs the grammars
        '''
        res = []
        for x in batch_predictions:
            rules = {}
            for S in self.cfg.rules:
                rules[S] = {}
                for P in self.cfg.rules[S]:
                    rules[S][P] = self.cfg.rules[S][P], \
                    float(x[self.RuleToIndex[(S, P)]])
            grammar = PCFG(
                start = self.cfg.start, 
                rules = rules, 
                max_program_depth = self.cfg.max_program_depth,
                clean = True)
            res.append(grammar)
        return res

    def ProgramEncoder(self, program, S = None, tensor = None):
        '''
        Outputs a tensor of dimension the number of transitions in the CFG
        with 1 for the transitions used to derive the program and 
        0 for the others
        '''
        if S == None:
            S = self.cfg.start

        if tensor == None:
            tensor = torch.zeros(self.output_dimension)

        if isinstance(program, Function):
            F = program.function
            args_P = program.arguments
            tensor[self.RuleToIndex[(S, F)]] = 1
            for i, arg in enumerate(args_P):
                self.ProgramEncoder(arg, self.cfg.rules[S][F][i], tensor)

        if isinstance(program, (BasicPrimitive, Variable)):
            tensor[self.RuleToIndex[(S, program)]] = 1

        assert(tensor.size() == (self.output_dimension,))
        return tensor

    def custom_collate(self, batch):
        return [batch[i][0] for i in range(len(batch))], torch.stack([batch[i][1] for i in range(len(batch))])

    # def custom_collate_2(batch):
    #     return [batch[i][0] for i in range(len(batch))], [batch[i][1] for i in range(len(batch))]



class LocalRulesPredictor(nn.Module):
    '''
    cfg: a cfg template
    IOEncoder: encode inputs and outputs
    IOEmbedder: embeds inputs and outputs
    size_hidden: size for hidden layers
    '''
    def __init__(self, 
        cfg, 
        IOEncoder,
        IOEmbedder,
        size_hidden,
        ):
        super(LocalRulesPredictor, self).__init__()

        self.cfg = cfg
        self.IOEncoder = IOEncoder
        self.IOEmbedder = IOEmbedder

        self.loss = lambda batch_grammar, batch_program:\
            - sum(grammar.log_probability_program(self.cfg.start, program)
                for grammar,program in zip(grammars, programs))
        self.optimizer = torch.optim.Adam(self.parameters(), lr=0.1)

        H = IOEncoder.output_dimension * self.IOEmbedder.output_dimension

        projection_layer = {}
        for S in self.cfg.rules:
            n_productions = len(self.cfg.rules[S])
            module = nn.Sequential(nn.Linear(H, n_productions),
                                   nn.LogSoftmax(-1))
            projection_layer[str(S)] = module
        self.projection_layer = nn.ModuleDict(projection_layer)
            
    def ProgramEncoder(self): 
        return lambda x: x

    def forward(self, batch_IOs):
        '''
        batch_IOs is a tensor of size
        (batch_size, IOEncoder.output_dimension, IOEmbedder.output_dimension) 
        '''
        grammars = []
        for i, x in enumerate(batch_IOs):
            print("size of x", x.size())
            x = self.IOEmbedder.forward_IOs(x)
            print("size of x", x.size())
            probabilities = {S: self.projection_layer[format(S)](x)
                             for S in self.cfg.rules}

            rules = {}
            for S in self.cfg.rules:
                rules[S] = {}
                for j, P in enumerate(self.cfg.rules[S]):
                    rules[S][P] = self.cfg.rules[S][P], \
                    probabilities[S][i, j]
            grammar = LogProbPCFG(self.cfg.start, 
                rules, 
                max_program_depth=self.cfg.max_program_depth)
            grammar.clean()
            grammars.append(grammar)
        return grammars

    def custom_collate(self, batch):
        return [batch[i][0] for i in range(len(batch))], torch.stack([batch[i][1] for i in range(len(batch))])








