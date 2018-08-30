"""
*******************************************************
 * Copyright (C) 2017 MindsDB Inc. <copyright@mindsdb.com>
 *
 * This file is part of MindsDB Server.
 *
 * MindsDB Server can not be copied and/or distributed without the express
 * permission of MindsDB Inc
 *******************************************************
"""
# import logging
from libs.helpers.logging import logging
from numpy import linalg as LA

import torch
import torch.nn as nn
from torch import optim
from torch.autograd import Variable
from scipy import stats

import numpy as np

from config import USE_CUDA
from libs.constants.mindsdb import *
from libs.ml_models.pytorch.libs.torch_helpers import arrayToFloatVariable, variableToArray
from libs.ml_models.pytorch.libs.torch_helpers import getTorchObjectBinary, storeTorchObject, getStoredTorchObject, RMSELoss

from libs.data_types.trainer_response import TrainerResponse
from libs.data_types.tester_response import TesterResponse
from libs.data_types.file_saved_response import FileSavedResponse
from libs.helpers.norm_denorm_helpers import denorm

class BaseModel(nn.Module):

    variable_wrapper = arrayToFloatVariable
    variable_unwrapper = variableToArray
    ignore_types = [DATA_TYPES.FULL_TEXT]

    def __init__(self, sample_batch, **kwargs):
        """

        :param sample_batch:
        :param use_cuda:
        :param kwargs:
        """
        super(BaseModel, self).__init__()

        self.lossFunction = torch.nn.MSELoss()
        self.errorFunction = torch.nn.MSELoss()
        self.sample_batch = sample_batch

        # self.learning_rates = [(0.09, 100), (0.1, 100), (0.05, 100), (0.08, 100), (0.09, 100), (0.04, 100), (0.07, 100), (0.08, 100), (0.03, 100)] #experiment 70%
        # self.learning_rates =  [(0.1, 100), (0.3, 100), (0.09, 100), (0.1, 100), (0.05, 100),(0.001,100)] ##Jorge, git
        self.learning_rates = [(0.1, 300), (0.01, 300), (0.001, 300),(0.05, 300), (0.005, 300),(0.1, 300), (0.01, 300), (0.001, 300)]
        #
        # self.learning_rates = [(0.01,40)]
        self.setLearningRateIndex(0)

        self.latest_file_id = None

        self.flatTarget = True
        self.flatInput = True
        self.optimizer = None
        self.optimizer_class = optim.Adam
        self.setup(sample_batch,  **kwargs)


    def zeroGradOptimizer(self):
        """

        :return:
        """
        if self.optimizer is None:
            self.optimizer = self.optimizer_class(self.parameters(), lr=self.current_learning_rate)
        self.optimizer.zero_grad()

    def setLearningRateIndex(self, index):
        """
        This updates the pointers in the learning rates
        :param index: the index
        :return:
        """
        if index >= len(self.learning_rates):
            index = len(self.learning_rates) -1
            logging.warning('Trying to set the learning rate on an index greater than learnign rates available')

        self.current_learning_rate_index = index
        self.total_epochs = self.learning_rates[self.current_learning_rate_index][EPOCHS_INDEX]
        self.current_learning_rate = self.learning_rates[self.current_learning_rate_index][LEARNING_RATE_INDEX]

    def optimize(self):
        """

        :return:
        """

        if self.optimizer is None:
            self.optimizer = self.optimizer_class(self.parameters(), lr=self.current_learning_rate)
        self.optimizer.step()


    def calculateBatchLoss(self, batch):
        """

        :param batch:
        :return:
        """

        predicted_target = self.forward(batch.getInput(flatten=self.flatInput))
        real_target = batch.getTarget(flatten=self.flatTarget)
        loss = self.lossFunction(predicted_target, real_target)
        batch_size = real_target.size()[0]
        return loss, batch_size


    def saveToDisk(self):
        """

        :return:
        """
        sample_batch = self.sample_batch
        self.sample_batch = None
        file_id, path = storeTorchObject(self)
        self.latest_file_id = file_id
        self.sample_batch = sample_batch
        return [FileSavedResponse(file_id, path)]

    @staticmethod
    def loadFromDisk(file_ids):
        """

        :param file_ids:
        :return:
        """
        return getStoredTorchObject(file_ids[0])


    def getLatestFromDisk(self):
        """

        :return:
        """
        return getStoredTorchObject(self.latest_file_id)


    def testModel(self, test_sampler):
        """

        :param test_sampler:
        :return:  TesterResponse
        """


        real_target_all = []
        predicted_target_all = []

        for batch_number, batch in enumerate(test_sampler):
            logging.info('[EPOCH-BATCH] testing batch: {batch_number}'.format(batch_number=batch_number))
            # get real and predicted values by running the model with the input of this batch
            predicted_target = self.forward(batch.getInput(flatten=self.flatInput))
            real_target = batch.getTarget(flatten=self.flatTarget)
            # append to all targets and all real values
            real_target_all += real_target.data.tolist()
            predicted_target_all += predicted_target.data.tolist()

        # caluclate the error for all values
        predicted_targets = self.sample_batch.deflatTarget(np.array(predicted_target_all))
        real_targets = self.sample_batch.deflatTarget(np.array(real_target_all))

        r_values = {}
        # calculate r and other statistical properties of error
        for target_key in real_targets:
            SSE = LA.norm(np.sum((real_targets[target_key]-predicted_targets[target_key])**2))
            SSrr = LA.norm(np.sum((real_targets[target_key]-np.mean(real_targets[target_key], axis=0))**2))
            if SSrr == 0:
                SSrr = 0.0000001
            r_values[target_key] =  (1 - SSE/SSrr)


        # calculate error using error function
        errors = {target_key: float(self.errorFunction(Variable(torch.FloatTensor(predicted_targets[target_key])), Variable(torch.FloatTensor(real_targets[target_key]))).data[0]) for target_key in real_targets}
        error = np.average([errors[key] for key in errors])
        r_value = np.average([r_values[key]**2 for key in r_values])

        resp = TesterResponse(
            error = error,
            accuracy= r_value,
            predicted_targets = predicted_targets,
            real_targets = real_targets
        )

        return resp


    def trainModel(self, train_sampler, learning_rate_index = None):
        """
        This function is an interator to train over the sampler

        :param train_sampler: the sampler to iterate over and train on

        :yield: TrainerResponse
        """

        model_object = self
        response = TrainerResponse(model_object)

        if learning_rate_index is not None:
            self.setLearningRateIndex(learning_rate_index)

        model_object.optimizer = None

        for epoch in range(self.total_epochs):

            full_set_loss = 0
            total_samples = 0
            response.epoch = epoch
            # train epoch
            for batch_number, batch in enumerate(train_sampler):
                response.batch = batch_number
                logging.info('[EPOCH-BATCH] Training on epoch: {epoch}/{num_epochs}, batch: {batch_number}'.format(
                        epoch=epoch + 1, num_epochs=self.total_epochs, batch_number=batch_number))

                model_object.zeroGradOptimizer()
                loss, batch_size = model_object.calculateBatchLoss(batch)
                if batch_size <= 0:
                    break
                total_samples += batch_size
                full_set_loss += int(loss.item()) * batch_size # this is because we need to wight the error by samples in batch
                average_loss = full_set_loss / total_samples
                loss.backward()
                model_object.optimize()
                response.loss = average_loss

                yield response



    # #############
    # METHODS TO IMPLEMENT BY CHILDREN
    # #############

    def setup(self, sample_batch, **kwargs):
        """
        this is what is called when the model object is instantiated
        :param sample_batch:
        :param use_cuda:
        :return:
        """
        logging.error('You must define a setup method for this model')
        pass

    def forward(self, input):
        """
        This is what is called when the model is forwarded
        :param input:
        :return:
        """
        logging.error('You must define a forward method for this model')
        pass





