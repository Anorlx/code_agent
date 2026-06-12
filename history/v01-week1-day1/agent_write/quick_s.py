def quick_sort(arr):
    """
    快速排序算法实现
    
    参数:
        arr: 待排序的列表
    
    返回:
        排序后的列表
    """
    # 基线条件：空列表或单元素列表已经是排序好的
    if len(arr) <= 1:
        return arr
    
    # 选择基准元素（这里选择中间元素）
    pivot = arr[len(arr) // 2]
    
    # 分区：将元素分为小于、等于、大于基准的三部分
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    # 递归排序并合并结果
    return quick_sort(left) + middle + quick_sort(right)


def quick_sort_inplace(arr, low=0, high=None):
    """
    原地快速排序实现（不占用额外空间）
    
    参数:
        arr: 待排序的列表
        low: 起始索引
        high: 结束索引
    """
    if high is None:
        high = len(arr) - 1
    
    if low < high:
        # 分区操作，返回基准元素的最终位置
        pivot_index = partition(arr, low, high)
        
        # 递归排序基准左右两边的子数组
        quick_sort_inplace(arr, low, pivot_index - 1)
        quick_sort_inplace(arr, pivot_index + 1, high)


def partition(arr, low, high):
    """
    分区函数：将数组分为小于和大于基准的两部分
    """
    # 选择最右边的元素作为基准
    pivot = arr[high]
    i = low - 1  # i 是小于基准元素的边界索引
    
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]  # 交换元素
    
    # 将基准元素放到正确的位置
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


# 测试代码
if __name__ == "__main__":
    # 测试数据
    test_arr1 = [64, 34, 25, 12, 22, 11, 90]
    test_arr2 = [64, 34, 25, 12, 22, 11, 90]
    
    print("原始数组:", test_arr1)
    
    # 方法1：返回新数组
    sorted_arr1 = quick_sort(test_arr1)
    print("快速排序（新数组）:", sorted_arr1)
    
    # 方法2：原地排序
    quick_sort_inplace(test_arr2)
    print("快速排序（原地）:", test_arr2)