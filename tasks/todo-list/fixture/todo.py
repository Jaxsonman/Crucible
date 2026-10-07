"""To-do list stored in a JSON file."""


class TodoList:
    def __init__(self, path):
        raise NotImplementedError

    def add(self, title):
        raise NotImplementedError

    def complete(self, item_id):
        raise NotImplementedError

    def delete(self, item_id):
        raise NotImplementedError

    def items(self, include_done=True):
        raise NotImplementedError
