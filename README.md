# Dynamic Load Balancing in SDN using Reinforcement Learning

This project implements **dynamic load balancing in Software Defined Networks (SDN)** using **Q-Learning**, along with **Round-Robin (RR) and Random** approaches for comparison. It uses **Mininet** for network emulation and **Ryu** as the SDN controller.

---

## Features
- SDN load balancing with **Q-Learning** (reinforcement learning)
- Baseline approaches: **Round-Robin** and **Random**
- Mininet topology (e.g., FatTree) for testing
- Real-time metrics collection: link utilization, packet drops
- Result visualization with Python plots

---

## Project Structure
sdn_proj/
├─ mininet_topo/ # Custom Mininet topology scripts
│ └─ my_topology.py
├─ controller/ # Ryu controllers
│ ├─ ryu_qlearning_lb.py
│ ├─ ryu_roundrobin_lb.py
│ └─ ryu_random_lb.py
├─ bash_scripts/ # Scripts to start Mininet and controllers
│ └─ sdn_fixed.sh
├─ plot_rl_results.py # Plot Q-Learning results
├─ plot_rr_results.py # Plot RR results
└─ README.md


---

## Requirements
- Python 3.x
- Mininet
- Ryu SDN framework
- Matplotlib, Pandas (for plotting results)

Install Python dependencies:
```bash
pip install matplotlib pandas
Install Ryu:

pip install ryu

## Setup and Run
Start the controller
Example for Q-Learning controller:

ryu-manager controller/ryu_qlearning_lb.py
Run Mininet topology

sudo python mininet_topo/my_topology.py
Collect traffic metrics
The controllers automatically monitor port stats and calculate rewards (for Q-Learning).

Visualize results

python plot_rl_results.py
python plot_rr_results.py

## Controllers
ryu_qlearning_lb.py → Q-Learning dynamic load balancing

ryu_roundrobin_lb.py → Round-Robin load balancing

ryu_random_lb.py → Random neighbor selection

## Visualization
Generates plots for link utilization, packet drops, and reward over time.

Compare Q-Learning vs RR vs Random to see performance improvements.

## Contributing
Fork the repository

Create a new branch (git checkout -b feature-branch)

Commit your changes (git commit -m "Add new feature")

Push (git push origin feature-branch)

Create a Pull Request

