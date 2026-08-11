# 计算 1 到 100 的和

def calculate_sum(start, end):
    """计算从 start 到 end 的整数和"""
    return (start + end) * (end - start + 1) // 2

if __name__ == "__main__":
    # 方法1：使用公式
    result_formula = calculate_sum(1, 100)
    print(f"使用公式计算 1-100 的和: {result_formula}")
    
    # 方法2：使用循环（验证）
    result_loop = sum(range(1, 101))
    print(f"使用循环计算 1-100 的和: {result_loop}")
    
    print(f"\n结果验证: {'✓ 一致' if result_formula == result_loop else '✗ 不一致'}")