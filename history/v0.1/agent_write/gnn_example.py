"""
简单的图神经网络 (GNN) 示例
使用 PyTorch Geometric 实现节点分类任务
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.nn import GCNConv
from torch_geometric.utils import to_networkx
import networkx as nx
import matplotlib.pyplot as plt

# 确保使用 CPU
device = torch.device('cpu')
print(f"使用设备: {device}")


# ==================== 创建合成图数据 ====================
def create_synthetic_graph():
    """
    创建一个合成图数据集
    模拟一个引文网络：节点是论文，边是引用关系
    节点特征：论文的关键词向量
    节点标签：论文类别（0, 1, 2）
    """
    # 创建一个随机图，100个节点
    num_nodes = 100
    num_features = 16  # 每个节点的特征维度
    num_classes = 3    # 分类类别数
    
    # 随机生成节点特征
    x = torch.randn((num_nodes, num_features))
    
    # 随机生成边（稀疏连接）
    num_edges = 300
    edge_index = torch.randint(0, num_nodes, (2, num_edges))
    
    # 生成节点标签（基于特征的模式）
    labels = torch.zeros(num_nodes, dtype=torch.long)
    for i in range(num_nodes):
        # 简单规则：根据特征均值分配类别
        if x[i].mean() > 0.3:
            labels[i] = 0
        elif x[i].mean() > -0.3:
            labels[i] = 1
        else:
            labels[i] = 2
    
    # 创建训练/验证/测试掩码
    train_mask = torch.zeros(num_nodes, dtype=torch.bool)
    val_mask = torch.zeros(num_nodes, dtype=torch.bool)
    test_mask = torch.zeros(num_nodes, dtype=torch.bool)
    
    train_mask[:60] = True      # 60个训练样本
    val_mask[60:80] = True      # 20个验证样本
    test_mask[80:] = True       # 20个测试样本
    
    data = Data(x=x, edge_index=edge_index, y=labels,
                train_mask=train_mask, val_mask=val_mask, test_mask=test_mask)
    
    return data, num_features, num_classes


# ==================== 定义 GCN 模型 ====================
class GCN(nn.Module):
    """
    图卷积网络 (Graph Convolutional Network)
    两层GCN + 分类层
    """
    def __init__(self, num_features, hidden_dim, num_classes):
        super(GCN, self).__init__()
        self.conv1 = GCNConv(num_features, hidden_dim)
        self.conv2 = GCNConv(hidden_dim, num_classes)
        self.dropout = nn.Dropout(0.5)
        
    def forward(self, x, edge_index):
        # 第一层 GCN
        x = self.conv1(x, edge_index)
        x = F.relu(x)
        x = self.dropout(x)
        
        # 第二层 GCN
        x = self.conv2(x, edge_index)
        
        return F.log_softmax(x, dim=1)


# ==================== 训练和评估函数 ====================
def train(model, optimizer, data):
    """训练一个 epoch"""
    model.train()
    optimizer.zero_grad()
    
    # 前向传播
    out = model(data.x, data.edge_index)
    
    # 只计算训练集的损失
    loss = F.nll_loss(out[data.train_mask], data.y[data.train_mask])
    
    # 反向传播
    loss.backward()
    optimizer.step()
    
    return loss.item()


def evaluate(model, data, mask):
    """评估模型在指定掩码上的准确率"""
    model.eval()
    with torch.no_grad():
        out = model(data.x, data.edge_index)
        pred = out.argmax(dim=1)
        correct = (pred[mask] == data.y[mask]).sum().item()
        acc = correct / mask.sum().item()
    return acc


# ==================== 可视化函数 ====================
def visualize_graph(data, title="Graph Structure"):
    """可视化图结构"""
    G = to_networkx(data, to_undirected=True)
    
    plt.figure(figsize=(10, 8))
    pos = nx.spring_layout(G, seed=42)
    
    # 根据标签着色
    node_colors = [data.y[i].item() for i in range(data.num_nodes)]
    
    nx.draw(G, pos, node_color=node_colors, node_size=100,
            cmap=plt.cm.Set3, with_labels=False, edge_color='gray', alpha=0.6)
    
    plt.title(title)
    plt.savefig('graph_visualization.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("图结构已保存到 graph_visualization.png")


def visualize_training(train_losses, val_accs, test_accs):
    """可视化训练过程"""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
    
    # 损失曲线
    ax1.plot(train_losses, 'b-', label='Train Loss')
    ax1.set_xlabel('Epoch')
    ax1.set_ylabel('Loss')
    ax1.set_title('Training Loss')
    ax1.legend()
    ax1.grid(True)
    
    # 准确率曲线
    ax2.plot(val_accs, 'g-', label='Val Accuracy')
    ax2.plot(test_accs, 'r-', label='Test Accuracy')
    ax2.set_xlabel('Epoch')
    ax2.set_ylabel('Accuracy')
    ax2.set_title('Accuracy')
    ax2.legend()
    ax2.grid(True)
    
    plt.tight_layout()
    plt.savefig('training_process.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("训练过程已保存到 training_process.png")


# ==================== 主程序 ====================
def main():
    print("=" * 60)
    print("图神经网络 (GCN) 节点分类示例")
    print("=" * 60)
    
    # 创建数据
    print("\n[1] 创建合成图数据...")
    data, num_features, num_classes = create_synthetic_graph()
    print(f"    节点数: {data.num_nodes}")
    print(f"    边数: {data.num_edges}")
    print(f"    特征维度: {num_features}")
    print(f"    类别数: {num_classes}")
    print(f"    训练集: {data.train_mask.sum().item()} 个节点")
    print(f"    验证集: {data.val_mask.sum().item()} 个节点")
    print(f"    测试集: {data.test_mask.sum().item()} 个节点")
    
    # 可视化图结构
    print("\n[2] 可视化图结构...")
    visualize_graph(data)
    
    # 创建模型
    print("\n[3] 初始化 GCN 模型...")
    model = GCN(num_features, hidden_dim=32, num_classes=num_classes).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)
    
    print(model)
    print(f"模型参数总数: {sum(p.numel() for p in model.parameters())}")
    
    # 训练模型
    print("\n[4] 开始训练...")
    epochs = 100
    train_losses = []
    val_accs = []
    test_accs = []
    
    for epoch in range(epochs):
        loss = train(model, optimizer, data)
        train_losses.append(loss)
        
        val_acc = evaluate(model, data, data.val_mask)
        test_acc = evaluate(model, data, data.test_mask)
        val_accs.append(val_acc)
        test_accs.append(test_acc)
        
        if (epoch + 1) % 10 == 0:
            print(f"    Epoch {epoch+1:3d} | Loss: {loss:.4f} | "
                  f"Val Acc: {val_acc:.4f} | Test Acc: {test_acc:.4f}")
    
    # 最终评估
    print("\n[5] 最终结果:")
    final_train_acc = evaluate(model, data, data.train_mask)
    final_val_acc = evaluate(model, data, data.val_mask)
    final_test_acc = evaluate(model, data, data.test_mask)
    
    print(f"    训练集准确率: {final_train_acc:.4f}")
    print(f"    验证集准确率: {final_val_acc:.4f}")
    print(f"    测试集准确率: {final_test_acc:.4f}")
    
    # 可视化训练过程
    print("\n[6] 保存训练过程可视化...")
    visualize_training(train_losses, val_accs, test_accs)
    
    print("\n" + "=" * 60)
    print("训练完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()