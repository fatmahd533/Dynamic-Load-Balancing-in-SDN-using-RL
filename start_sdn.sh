#!/bin/bash

echo "🚀 DÉMARRAGE SDN AVEC Q-LEARNING"

# Nettoyer d'abord
sudo pkill -f ryu-manager
sudo pkill -f python
sudo mn -c 2>/dev/null
sudo ovs-vsctl list-br | xargs -I {} sudo ovs-vsctl del-br {} 2>/dev/null

# Attendre que le nettoyage soit complet
sleep 3

# Vérifier l'installation de Ryu
if ! command -v ryu-manager &> /dev/null; then
    echo "Installation de Ryu..."
    pip3 install ryu
fi

# Vérifier NetworkX
if ! python3 -c "import networkx" 2>/dev/null; then
    echo "Installation de NetworkX..."
    pip3 install networkx
fi

# Démarrer le contrôleur en arrière-plan
echo "Démarrage du contrôleur Q-learning..."
ryu-manager ~/sdn_proj/controller/ryu_qlearning_lb.py --ofp-tcp-listen-port 6633 > ryu.log 2>&1 &
RYU_PID=$!
sleep 5

if ps -p $RYU_PID > /dev/null; then
    echo "✅ Contrôleur démarré (PID: $RYU_PID)"
    
    # Vérifier que le contrôleur écoute sur le port
    if netstat -tlnp 2>/dev/null | grep 6633 > /dev/null; then
        echo "✅ Contrôleur écoute sur le port 6633"
    else
        echo "❌ Le contrôleur n'écoute pas sur le port 6633"
        echo "Logs du contrôleur:"
        cat ryu.log
        exit 1
    fi
    
    echo ""
    echo "📟 Démarrage de Mininet..."
    echo "=== COMMANDES UTILES ==="
    echo "pingall          # Test de connectivité"
    echo "h1 ping -c 3 h3  # Test de chemin spécifique"
    echo "net              # Voir la topologie"
    echo "nodes            # Liste des nœuds"
    echo "exit             # Quitter"
    echo "========================"
    echo ""
    
    # Démarrer Mininet
    cd ~/sdn_proj
    sudo python3 topo.py
    
    # Nettoyer après arrêt
    echo "Nettoyage..."
    kill $RYU_PID 2>/dev/null
    sudo mn -c 2>/dev/null
else
    echo "❌ Échec du démarrage du contrôleur"
    echo "Logs d'erreur:"
    cat ryu.log
fi

echo "🎉 Session terminée!"
