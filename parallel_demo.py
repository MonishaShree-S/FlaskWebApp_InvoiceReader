from multiprocessing import Process
import threading
import os

def task(kind, name):
    print(f"Running {kind} {name} (pid={os.getpid()})", flush=True)

if __name__ == "__main__":
    # Start processes
    p1 = Process(target=task, args=("Process", "1"))
    p2 = Process(target=task, args=("Process", "2"))
    p1.start()
    p2.start()

    # Start threads
    t1 = threading.Thread(target=task, args=("Thread", "1"))
    t2 = threading.Thread(target=task, args=("Thread", "2"))
    t1.start()
    t2.start()

    # Wait for all to finish
    p1.join(); p2.join()
    t1.join(); t2.join()
